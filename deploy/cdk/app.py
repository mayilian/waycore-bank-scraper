#!/usr/bin/env python3
"""CDK app — two stacks: Foundation (infra) and App (services).

Deploy order:
    cdk deploy WayCoreFoundation   # VPC, RDS, ECR, S3, Restate
    # push Docker images to ECR
    # fill Secrets Manager
    cdk deploy WayCoreApp          # API + Worker services
"""

import aws_cdk as cdk
from waycore_stack import WayCoreApp, WayCoreFoundation

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region"),
)

foundation = WayCoreFoundation(app, "WayCoreFoundation", env=env)
WayCoreApp(app, "WayCoreApp", foundation=foundation, env=env)

app.synth()
