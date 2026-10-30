"""Morning starter for the trading machine (AWS Lambda, run by EventBridge Scheduler).

{"action": "start"}: start the machine. AWS sometimes has no spare machines of one size in the
zone (2026-10-30: t4g.medium refused 09:00-09:52), so when a size is refused, switch the stopped
machine to the next size in SIZES and try again. Every start first tries the preferred size, so a
fallback size lasts one day. Safe to run repeatedly: does nothing when the machine is already up.
{"action": "check"}: email the owner (SNS) if the machine is not running.
Test: {"action": "start", "refuse": ["t4g.medium"]} pretends AWS refused those sizes.
"""
import os
import time

import boto3
from botocore.exceptions import ClientError

INSTANCE = os.environ["INSTANCE_ID"]
TOPIC = os.environ["TOPIC_ARN"]
REGION = os.environ.get("AWS_REGION", "ap-south-1")
# All arm64 (the disk holds an arm64 system) with at least 2 vCPU and 4 GB; cheapest first,
# mixing families and generations so one shortage doesn't take out the whole list.
SIZES = os.environ.get("SIZES", "t4g.medium t4g.large c7g.large m6g.large c8g.large m7g.large").split()
NO_CAPACITY = {"InsufficientInstanceCapacity", "Unsupported", "InstanceLimitExceeded"}

ec2 = boto3.client("ec2")
sns = boto3.client("sns")


def describe():
    i = ec2.describe_instances(InstanceIds=[INSTANCE])["Reservations"][0]["Instances"][0]
    return i["State"]["Name"], i["InstanceType"]


def wait_stopped():
    for _ in range(60):
        state, size = describe()
        if state == "stopped":
            return size
        if state in ("pending", "running"):
            return None
        time.sleep(5)
    raise RuntimeError(f"{INSTANCE} stuck in {state}")


def try_size(size, current, refuse):
    if size != current:
        ec2.modify_instance_attribute(InstanceId=INSTANCE, InstanceType={"Value": size})
    if size in refuse:
        raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity", "Message": "test"}}, "StartInstances")
    ec2.start_instances(InstanceIds=[INSTANCE])


def start(refuse, deadline):
    tried = []
    while True:
        for size in SIZES:
            current = wait_stopped()
            if current is None:
                return {"result": "already running", "tried": tried}
            try:
                try_size(size, current, refuse)
                print(f"started as {size} after {tried}")
                return {"result": "started", "size": size, "tried": tried}
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code not in NO_CAPACITY:
                    raise
                tried.append(f"{size}: {code}")
                print(f"{size} refused: {code}")
        if time.time() > deadline:
            return {"result": "no capacity", "tried": tried}
        time.sleep(30)


def handler(event, context):
    action = event.get("action", "start")
    if action == "check":
        state, size = describe()
        if state != "running":
            sns.publish(TopicArn=TOPIC, Subject="Trading machine is NOT running",
                        Message=f"{INSTANCE} is {state} ({size}) at {time.strftime('%H:%M')} UTC. "
                                f"The 08:00-09:45 IST starter could not start it. Start it in the EC2 console "
                                f"({REGION}) or with: aws ec2 start-instances --region {REGION} "
                                f"--instance-ids {INSTANCE}")
        return {"state": state, "size": size}
    # Leave a minute of the Lambda's time for the last attempt.
    deadline = time.time() + context.get_remaining_time_in_millis() / 1000 - 90
    return start(set(event.get("refuse", [])), deadline)
