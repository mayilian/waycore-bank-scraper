"""App stack: API + Worker Fargate services.

Depends on Foundation stack for VPC, RDS, ECR, Secrets, Cluster, ALB.
Deploy after Docker images are pushed to ECR.
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_servicediscovery as sd,
)
from constructs import Construct

from .foundation_stack import WayCoreFoundation


class WayCoreApp(Stack):
    """App services: API + Worker on Fargate. Deploy after images are in ECR."""

    def __init__(
        self, scope: Construct, construct_id: str, foundation: WayCoreFoundation, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        restate_ingress_url = "http://restate.waycore.local:8080"

        shared_env = {
            "USE_RDS_PROXY": "false",
            "RESTATE_INGRESS_URL": restate_ingress_url,
        }
        shared_secrets = {
            "DB_HOST": ecs.Secret.from_secrets_manager(foundation.db.secret, "host"),
            "DB_PORT": ecs.Secret.from_secrets_manager(foundation.db.secret, "port"),
            "DB_USERNAME": ecs.Secret.from_secrets_manager(foundation.db.secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(foundation.db.secret, "password"),
            "DB_NAME": ecs.Secret.from_secrets_manager(foundation.db.secret, "dbname"),
            "ENCRYPTION_KEY": ecs.Secret.from_secrets_manager(foundation.secrets, "ENCRYPTION_KEY"),
        }

        # ---------------------------------------------------------------
        # API Service
        # ---------------------------------------------------------------
        api_task_def = ecs.FargateTaskDefinition(
            self, "ApiTaskDef", cpu=256, memory_limit_mib=512,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        api_container = api_task_def.add_container(
            "api",
            image=ecs.ContainerImage.from_ecr_repository(foundation.api_repo, tag="latest"),
            command=["uv", "run", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"],
            environment=shared_env, secrets=shared_secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=foundation.log_group),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)\" 2>/dev/null || exit 1"],
                interval=Duration.seconds(15), timeout=Duration.seconds(5),
                retries=3, start_period=Duration.seconds(60),
            ),
        )
        api_container.add_port_mappings(ecs.PortMapping(container_port=8000))

        api_service = ecs.FargateService(
            self, "ApiService", cluster=foundation.cluster,
            task_definition=api_task_def, desired_count=1,
            security_groups=[foundation.api_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            cloud_map_options=ecs.CloudMapOptions(name="api"),
            enable_execute_command=True,
        )

        # Create target group and listener rule in App stack (no cross-stack cycle)
        api_tg = elbv2.ApplicationTargetGroup(
            self, "ApiTg", vpc=foundation.vpc, port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[api_service],
            health_check=elbv2.HealthCheck(
                path="/healthz", interval=Duration.seconds(15),
                timeout=Duration.seconds(5), healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        elbv2.ApplicationListenerRule(
            self, "ApiListenerRule",
            listener=elbv2.ApplicationListener.from_application_listener_attributes(
                self, "ImportedListener",
                listener_arn=foundation.api_listener_arn,
                security_group=foundation.alb_sg,
            ),
            priority=1,
            conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
            target_groups=[api_tg],
        )

        # ---------------------------------------------------------------
        # Worker Service
        # ---------------------------------------------------------------
        worker_task_def = ecs.FargateTaskDefinition(
            self, "WorkerTaskDef", cpu=1024, memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        worker_secrets = {
            **shared_secrets,
            "ANTHROPIC_API_KEY": ecs.Secret.from_secrets_manager(foundation.secrets, "ANTHROPIC_API_KEY"),
        }
        worker_container = worker_task_def.add_container(
            "worker",
            image=ecs.ContainerImage.from_ecr_repository(foundation.worker_repo, tag="latest"),
            environment={**shared_env, "SCREENSHOT_BACKEND": "local", "PLAYWRIGHT_HEADFUL": "0"},
            secrets=worker_secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=foundation.log_group),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:9000/restate/health', timeout=3)\" 2>/dev/null || exit 1"],
                interval=Duration.seconds(15), timeout=Duration.seconds(5),
                retries=3, start_period=Duration.seconds(60),
            ),
        )
        worker_container.add_port_mappings(ecs.PortMapping(container_port=9000))

        worker_service = ecs.FargateService(
            self, "WorkerService", cluster=foundation.cluster,
            task_definition=worker_task_def, desired_count=1,
            security_groups=[foundation.worker_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(capacity_provider="FARGATE", base=1, weight=1),
                ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=4),
            ],
            cloud_map_options=ecs.CloudMapOptions(name="worker", dns_record_type=sd.DnsRecordType.A),
            enable_execute_command=True,
        )

        foundation.screenshots_bucket.grant_read_write(worker_task_def.task_role)

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        cdk.CfnOutput(self, "AlbUrl", value=foundation.alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "WorkerServiceArn", value=worker_service.service_arn)
