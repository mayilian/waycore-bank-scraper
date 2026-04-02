#!/usr/bin/env python3
"""CDK app entry point for WayCore bank scraper infrastructure."""

import aws_cdk as cdk
from waycore_stack import WayCoreStack

app = cdk.App()

WayCoreStack(
    app,
    "WayCoreStack",
    env=cdk.Environment(
        # Uses CLI-configured account/region by default.
        # Override with: cdk deploy --context account=123456789 --context region=us-east-1
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region"),
    ),
)

app.synth()
