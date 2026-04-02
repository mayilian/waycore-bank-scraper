"""Foundation stack: VPC, RDS, ECR, S3, Secrets, ECS Cluster, ALB, Restate.

Long-lived infrastructure deployed once. App stack depends on this.
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_rds as rds,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_servicediscovery as sd,
)
from constructs import Construct


class WayCoreFoundation(Stack):
    """Long-lived infrastructure: VPC, RDS, ECR, S3, Secrets, ECS Cluster, ALB, Restate."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # VPC
        # ---------------------------------------------------------------
        self.vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1)

        # ---------------------------------------------------------------
        # Security groups — all in Foundation to avoid cross-stack cycles
        # ---------------------------------------------------------------
        self.db_sg = ec2.SecurityGroup(self, "DbSg", vpc=self.vpc, description="RDS")
        self.ecs_sg = ec2.SecurityGroup(self, "EcsSg", vpc=self.vpc, description="Restate")
        self.api_sg = ec2.SecurityGroup(self, "ApiSg", vpc=self.vpc, description="API tasks")
        self.worker_sg = ec2.SecurityGroup(self, "WorkerSg", vpc=self.vpc, description="Worker tasks")
        self.alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=self.vpc, description="ALB")

        # ALB accepts HTTP from internet
        self.alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP from internet")
        # ALB → API
        self.api_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(8000), "ALB to API")
        # DB access
        self.db_sg.add_ingress_rule(self.api_sg, ec2.Port.tcp(5432), "API to Postgres")
        self.db_sg.add_ingress_rule(self.worker_sg, ec2.Port.tcp(5432), "Worker to Postgres")
        # API → Restate
        self.ecs_sg.add_ingress_rule(self.api_sg, ec2.Port.tcp(8080), "API to Restate")
        # Restate → Worker
        self.worker_sg.add_ingress_rule(self.ecs_sg, ec2.Port.tcp(9000), "Restate to Worker")
        # Worker → Restate (for callbacks)
        self.ecs_sg.add_ingress_rule(self.worker_sg, ec2.Port.tcp(8080), "Worker to Restate")

        # ---------------------------------------------------------------
        # Secrets Manager
        # ---------------------------------------------------------------
        self.secrets = secretsmanager.Secret(
            self,
            "WayCoreSecrets",
            secret_name="waycore/secrets",
            description="WayCore app secrets — fill after deploy",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"ENCRYPTION_KEY":"CHANGE_ME","ANTHROPIC_API_KEY":"CHANGE_ME"}',
                generate_string_key="_random",
            ),
        )

        # ---------------------------------------------------------------
        # RDS PostgreSQL
        # ---------------------------------------------------------------
        self.db = rds.DatabaseInstance(
            self,
            "Postgres",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_12,
            ),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.db_sg],
            database_name="waycore",
            credentials=rds.Credentials.from_generated_secret("waycore"),
            allocated_storage=20,
            max_allocated_storage=50,
            storage_encrypted=True,
            multi_az=False,
            backup_retention=Duration.days(7),
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # S3 bucket for screenshots
        # ---------------------------------------------------------------
        self.screenshots_bucket = s3.Bucket(
            self,
            "ScreenshotsBucket",
            bucket_name=f"waycore-screenshots-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )

        # ---------------------------------------------------------------
        # ECR repositories
        # ---------------------------------------------------------------
        self.api_repo = ecr.Repository(
            self, "ApiRepo", repository_name="waycore-api",
            removal_policy=RemovalPolicy.DESTROY, empty_on_delete=True,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )
        self.worker_repo = ecr.Repository(
            self, "WorkerRepo", repository_name="waycore-worker",
            removal_policy=RemovalPolicy.DESTROY, empty_on_delete=True,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )

        # ---------------------------------------------------------------
        # ECS Cluster + Cloud Map
        # ---------------------------------------------------------------
        self.cluster = ecs.Cluster(
            self, "Cluster", vpc=self.vpc, cluster_name="waycore",
            container_insights_v2=ecs.ContainerInsights.ENABLED,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name="waycore.local", type=sd.NamespaceType.DNS_PRIVATE,
            ),
        )

        self.log_group = logs.LogGroup(
            self, "Logs", log_group_name="/ecs/waycore",
            retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # ALB — lives in Foundation so App stack has no cross-stack SG cycle
        # ---------------------------------------------------------------
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", vpc=self.vpc, internet_facing=True, security_group=self.alb_sg,
        )
        self.api_listener = self.alb.add_listener("HttpListener", port=80, open=False)
        self.api_listener.add_action(
            "Default",
            action=elbv2.ListenerAction.fixed_response(
                status_code=503, content_type="text/plain",
                message_body="API not deployed yet",
            ),
        )
        self.api_listener_arn = self.api_listener.listener_arn

        # ---------------------------------------------------------------
        # Restate server
        # ---------------------------------------------------------------
        restate_task_def = ecs.FargateTaskDefinition(
            self, "RestateTaskDef", cpu=512, memory_limit_mib=1024,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        restate_container = restate_task_def.add_container(
            "restate",
            image=ecs.ContainerImage.from_registry("docker.io/restatedev/restate:1.6"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="restate", log_group=self.log_group),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -sf http://localhost:9070/health || exit 1"],
                interval=Duration.seconds(15), timeout=Duration.seconds(5),
                retries=3, start_period=Duration.seconds(15),
            ),
        )
        restate_container.add_port_mappings(
            ecs.PortMapping(container_port=8080, name="ingress"),
            ecs.PortMapping(container_port=9070, name="admin"),
        )
        ecs.FargateService(
            self, "RestateService", cluster=self.cluster,
            task_definition=restate_task_def, desired_count=1,
            security_groups=[self.ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            cloud_map_options=ecs.CloudMapOptions(name="restate", dns_record_type=sd.DnsRecordType.A),
            enable_execute_command=True,
        )

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        cdk.CfnOutput(self, "AlbUrl", value=self.alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "DbEndpoint", value=self.db.db_instance_endpoint_address)
        cdk.CfnOutput(self, "SecretsArn", value=self.secrets.secret_arn)
        cdk.CfnOutput(self, "ApiRepoUri", value=self.api_repo.repository_uri)
        cdk.CfnOutput(self, "WorkerRepoUri", value=self.worker_repo.repository_uri)
        cdk.CfnOutput(self, "ScreenshotsBucketName", value=self.screenshots_bucket.bucket_name)
