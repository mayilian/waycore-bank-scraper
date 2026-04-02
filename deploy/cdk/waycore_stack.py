"""
WayCore Bank Scraper — single-stack CDK deployment.

Creates: VPC, RDS PostgreSQL, ECS Cluster, ECR repo, ALB, Secrets Manager,
three Fargate services (API, Worker, Restate server).

Usage:
    cd deploy/cdk
    pip install -r requirements.txt
    cdk deploy
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
    aws_ecs_patterns as ecs_patterns,
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


class WayCoreStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # VPC — 2 AZs, public + private subnets. NAT gateway for egress.
        # ---------------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,  # Keep costs low; bump to 2 for HA.
        )

        # ---------------------------------------------------------------
        # Security groups
        # ---------------------------------------------------------------
        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, description="RDS PostgreSQL")
        ecs_sg = ec2.SecurityGroup(self, "EcsSg", vpc=vpc, description="ECS services")

        # ECS -> RDS
        db_sg.add_ingress_rule(ecs_sg, ec2.Port.tcp(5432), "ECS to Postgres")
        # ECS services talk to each other (Restate <-> Worker, API -> Restate)
        ecs_sg.add_ingress_rule(ecs_sg, ec2.Port.all_tcp(), "Inter-service traffic")

        # ---------------------------------------------------------------
        # Secrets Manager — sensitive env vars, populated manually or via CLI.
        # CDK creates the secret shells; you fill in the values post-deploy.
        # ---------------------------------------------------------------
        secrets = secretsmanager.Secret(
            self,
            "WayCoreSecrets",
            secret_name="waycore/secrets",
            description="WayCore app secrets (DATABASE_URL auto-populated, rest manual)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"ENCRYPTION_KEY":"CHANGE_ME","ANTHROPIC_API_KEY":"CHANGE_ME"}',
                generate_string_key="_random",  # Forces creation; ignore this key.
            ),
        )

        # ---------------------------------------------------------------
        # RDS PostgreSQL — t4g.micro, single-AZ, encrypted.
        # ---------------------------------------------------------------
        db = rds.DatabaseInstance(
            self,
            "Postgres",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_4,
            ),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[db_sg],
            database_name="waycore",
            credentials=rds.Credentials.from_generated_secret("waycore"),
            allocated_storage=20,
            max_allocated_storage=50,  # Auto-scale storage.
            storage_encrypted=True,
            multi_az=False,  # Single-AZ for cost. Flip for prod.
            backup_retention=Duration.days(7),
            deletion_protection=False,  # Set True for prod.
            removal_policy=RemovalPolicy.SNAPSHOT,
        )

        # ---------------------------------------------------------------
        # S3 bucket for screenshots
        # ---------------------------------------------------------------
        screenshots_bucket = s3.Bucket(
            self,
            "ScreenshotsBucket",
            bucket_name=f"waycore-screenshots-{self.account}",
            removal_policy=RemovalPolicy.RETAIN,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(30)),  # Screenshots are debug artifacts.
            ],
        )

        # ---------------------------------------------------------------
        # ECR repositories — one per service (API is slim, Worker has Playwright).
        # ---------------------------------------------------------------
        api_repo = ecr.Repository(
            self,
            "ApiRepo",
            repository_name="waycore-api",
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )
        worker_repo = ecr.Repository(
            self,
            "WorkerRepo",
            repository_name="waycore-worker",
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )

        # ---------------------------------------------------------------
        # ECS Cluster with Cloud Map namespace for service discovery.
        # ---------------------------------------------------------------
        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            cluster_name="waycore",
            container_insights_v2=ecs.ContainerInsights.ENABLED,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name="waycore.local",
                type=sd.NamespaceType.DNS_PRIVATE,
            ),
        )

        # Shared log group — all services log here with different prefixes.
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name="/ecs/waycore",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # Helper: build DATABASE_URL from RDS secret fields.
        # RDS generated secret has: username, password, host, port, dbname.
        # We can't compose a connection string natively in CDK, so we pass
        # the secret ARN and reconstruct in the container entrypoint, OR
        # we pass individual fields. Here we pass the RDS secret directly
        # and let the app parse it (src/db/session.py handles USE_RDS_PROXY).
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Restate server — runs as its own Fargate service.
        # Port 8080 = ingress (receives workflow calls from API).
        # Port 9070 = admin (registers worker endpoints).
        # Discovered via Cloud Map: restate.waycore.local
        # ---------------------------------------------------------------
        restate_task_def = ecs.FargateTaskDefinition(
            self,
            "RestateTaskDef",
            cpu=512,
            memory_limit_mib=1024,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        restate_container = restate_task_def.add_container(
            "restate",
            image=ecs.ContainerImage.from_registry("docker.io/restatedev/restate:1.1"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="restate",
                log_group=log_group,
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -sf http://localhost:9070/health || exit 1"],
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(15),
            ),
        )

        restate_container.add_port_mappings(
            ecs.PortMapping(container_port=8080, name="ingress"),
            ecs.PortMapping(container_port=9070, name="admin"),
        )

        ecs.FargateService(
            self,
            "RestateService",
            cluster=cluster,
            task_definition=restate_task_def,
            desired_count=1,
            security_groups=[ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            cloud_map_options=ecs.CloudMapOptions(
                name="restate",  # restate.waycore.local
                dns_record_type=sd.DnsRecordType.A,
            ),
            enable_execute_command=True,
        )

        restate_ingress_url = "http://restate.waycore.local:8080"
        restate_admin_url = "http://restate.waycore.local:9070"

        # ---------------------------------------------------------------
        # Shared ECS environment + secrets for API and Worker.
        # ---------------------------------------------------------------
        shared_env = {
            "USE_RDS_PROXY": "true",
            "RESTATE_INGRESS_URL": restate_ingress_url,
        }

        # Pass RDS connection info as individual secret fields.
        # The app's session.py builds the URL from these when USE_RDS_PROXY=true.
        shared_secrets = {
            "DB_HOST": ecs.Secret.from_secrets_manager(db.secret, "host"),
            "DB_PORT": ecs.Secret.from_secrets_manager(db.secret, "port"),
            "DB_USERNAME": ecs.Secret.from_secrets_manager(db.secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(db.secret, "password"),
            "DB_NAME": ecs.Secret.from_secrets_manager(db.secret, "dbname"),
            "ENCRYPTION_KEY": ecs.Secret.from_secrets_manager(secrets, "ENCRYPTION_KEY"),
        }

        # ---------------------------------------------------------------
        # API Service — ALB-fronted, minimal resources, ARM64.
        # ---------------------------------------------------------------
        api_task_def = ecs.FargateTaskDefinition(
            self,
            "ApiTaskDef",
            cpu=256,
            memory_limit_mib=512,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        api_container = api_task_def.add_container(
            "api",
            image=ecs.ContainerImage.from_ecr_repository(api_repo, tag="latest"),
            command=[
                "uv",
                "run",
                "uvicorn",
                "src.api.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            environment=shared_env,
            secrets=shared_secrets,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="api",
                log_group=log_group,
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -sf http://localhost:8000/healthz || exit 1"],
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(10),
            ),
        )

        api_container.add_port_mappings(
            ecs.PortMapping(container_port=8000),
        )

        api_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "ApiService",
            cluster=cluster,
            task_definition=api_task_def,
            desired_count=1,
            public_load_balancer=True,
            security_groups=[ecs_sg],
            # ALB health check hits the FastAPI healthz endpoint.
            health_check_grace_period=Duration.seconds(30),
            enable_execute_command=True,
            cloud_map_options=ecs.CloudMapOptions(
                name="api",  # api.waycore.local
            ),
        )

        # Tighten ALB health check to match the container's.
        api_service.target_group.configure_health_check(
            path="/healthz",
            interval=Duration.seconds(15),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # ---------------------------------------------------------------
        # Worker Service — 1 vCPU, 2GB, Spot (Graviton ARM64).
        # No ALB — Restate calls Worker directly via Cloud Map.
        # ---------------------------------------------------------------
        worker_task_def = ecs.FargateTaskDefinition(
            self,
            "WorkerTaskDef",
            cpu=1024,
            memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        worker_secrets = {
            **shared_secrets,
            "ANTHROPIC_API_KEY": ecs.Secret.from_secrets_manager(secrets, "ANTHROPIC_API_KEY"),
        }

        worker_container = worker_task_def.add_container(
            "worker",
            image=ecs.ContainerImage.from_ecr_repository(worker_repo, tag="latest"),
            # CMD from Dockerfile is used (hypercorn).
            environment={
                **shared_env,
                "SCREENSHOT_BACKEND": "s3",
                "S3_BUCKET": screenshots_bucket.bucket_name,
                "PLAYWRIGHT_HEADFUL": "0",
            },
            secrets=worker_secrets,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="worker",
                log_group=log_group,
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -sf http://localhost:9000/restate/health || exit 1"],
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(30),  # Playwright install takes a moment.
            ),
        )

        worker_container.add_port_mappings(
            ecs.PortMapping(container_port=9000),
        )

        ecs.FargateService(
            self,
            "WorkerService",
            cluster=cluster,
            task_definition=worker_task_def,
            desired_count=1,
            security_groups=[ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            capacity_provider_strategies=[
                # Spot for cost savings. Worker is stateless — Restate retries on eviction.
                ecs.CapacityProviderStrategy(
                    capacity_provider="FARGATE_SPOT",
                    weight=1,
                ),
            ],
            cloud_map_options=ecs.CloudMapOptions(
                name="worker",  # worker.waycore.local
                dns_record_type=sd.DnsRecordType.A,
            ),
            enable_execute_command=True,
        )

        # Worker needs S3 access for screenshots.
        screenshots_bucket.grant_read_write(worker_task_def.task_role)

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        cdk.CfnOutput(self, "AlbUrl", value=api_service.load_balancer.load_balancer_dns_name)
        cdk.CfnOutput(self, "DbEndpoint", value=db.db_instance_endpoint_address)
        cdk.CfnOutput(self, "SecretsArn", value=secrets.secret_arn)
        cdk.CfnOutput(self, "ApiRepoUri", value=api_repo.repository_uri)
        cdk.CfnOutput(self, "WorkerRepoUri", value=worker_repo.repository_uri)
        cdk.CfnOutput(self, "ScreenshotsBucket", value=screenshots_bucket.bucket_name)
        cdk.CfnOutput(
            self,
            "RegisterWorkerCmd",
            value=f"curl -X POST {restate_admin_url}/deployments -H 'content-type: application/json' -d '{{\"uri\": \"http://worker.waycore.local:9000\"}}'",
            description="Run after deploy to register the Worker with Restate",
        )
