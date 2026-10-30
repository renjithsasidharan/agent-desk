#!/bin/bash
# Install or update the machine's weekday schedule, from a computer with the AWS CLI logged in:
#   bash deploy/aws/install-starter.sh <instance-id> <alert email>
#   (AWS_REGION, default ap-south-1; DESK_NAME, default agent-desk, prefixes every AWS name)
# Schedules (Asia/Kolkata, Mon-Fri): <name>-start runs the starter (starter.py) every 15 min
# 08:00-09:45 (no-op once the machine is up), <name>-check emails at 08:45 if it is still down,
# <name>-stop stops it at 16:00.
set -euo pipefail
INSTANCE=${1:?usage: install-starter.sh <instance-id> <alert email>}
EMAIL=${2:?usage: install-starter.sh <instance-id> <alert email>}
REGION=${AWS_REGION:-ap-south-1}
NAME=${DESK_NAME:-agent-desk}
FN=$NAME-starter
ACCT=$(aws sts get-caller-identity --query Account --output text)
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
aws() { command aws --region "$REGION" "$@"; }

# Alerts: an SNS topic emailing the owner (AWS sends a confirmation link first).
TOPIC=$(aws sns create-topic --name $NAME-alerts --tags Key=Project,Value=$NAME --query TopicArn --output text)
if ! aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" --query 'Subscriptions[].Endpoint' --output text | grep -qw "$EMAIL"; then
    aws sns subscribe --topic-arn "$TOPIC" --protocol email --notification-endpoint "$EMAIL" >/dev/null
    echo "confirmation email sent to $EMAIL"
fi

# The function's role: start and resize this one machine, send alerts, write its log.
ROLE=$NAME-starter
if ! aws iam get-role --role-name $ROLE >/dev/null 2>&1; then
    aws iam create-role --role-name $ROLE --tags Key=Project,Value=$NAME --assume-role-policy-document \
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
    sleep 10   # a new role takes a moment before Lambda accepts it
fi
aws iam put-role-policy --role-name $ROLE --policy-name starter --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": \"ec2:DescribeInstances\", \"Resource\": \"*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"ec2:StartInstances\", \"ec2:ModifyInstanceAttribute\"],
     \"Resource\": \"arn:aws:ec2:$REGION:$ACCT:instance/$INSTANCE\"},
    {\"Effect\": \"Allow\", \"Action\": \"sns:Publish\", \"Resource\": \"$TOPIC\"},
    {\"Effect\": \"Allow\", \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"],
     \"Resource\": \"arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda/$FN:*\"}
  ]}"

(cd "$HERE" && zip -q "$TMP/starter.zip" starter.py)
ENV="Variables={INSTANCE_ID=$INSTANCE,TOPIC_ARN=$TOPIC}"
if aws lambda get-function --function-name $FN >/dev/null 2>&1; then
    aws lambda update-function-code --function-name $FN --zip-file "fileb://$TMP/starter.zip" >/dev/null
    aws lambda wait function-updated --function-name $FN
    aws lambda update-function-configuration --function-name $FN --environment "$ENV" --timeout 600 >/dev/null
else
    aws lambda create-function --function-name $FN --runtime python3.12 --architectures arm64 \
        --handler starter.handler --role "arn:aws:iam::$ACCT:role/$ROLE" --timeout 600 --memory-size 128 \
        --environment "$ENV" --zip-file "fileb://$TMP/starter.zip" --tags Project=$NAME >/dev/null
fi
aws lambda wait function-updated --function-name $FN
aws logs create-log-group --log-group-name /aws/lambda/$FN 2>/dev/null || true
aws logs put-retention-policy --log-group-name /aws/lambda/$FN --retention-in-days 90
FN_ARN=$(aws lambda get-function --function-name $FN --query Configuration.FunctionArn --output text)

# The scheduler's role: run the starter and stop this one machine.
SROLE=$NAME-scheduler
if ! aws iam get-role --role-name $SROLE >/dev/null 2>&1; then
    aws iam create-role --role-name $SROLE --tags Key=Project,Value=$NAME --assume-role-policy-document \
        "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"scheduler.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCT\"}}}]}" >/dev/null
fi
aws iam put-role-policy --role-name $SROLE --policy-name start-stop --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"ec2:StartInstances\",\"ec2:StopInstances\"],\"Resource\":\"arn:aws:ec2:$REGION:$ACCT:instance/$INSTANCE\"}]}"
aws iam put-role-policy --role-name $SROLE --policy-name invoke-starter --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$FN_ARN\"}]}"
sleep 10

schedule() {  # name, cron, input[, target ARN]
    local target="{\"Arn\":\"${4:-$FN_ARN}\",\"RoleArn\":\"arn:aws:iam::$ACCT:role/$SROLE\",\"Input\":$(printf '%s' "$3" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"RetryPolicy\":{\"MaximumRetryAttempts\":2,\"MaximumEventAgeInSeconds\":600}}"
    local verb=create-schedule
    aws scheduler get-schedule --name "$1" >/dev/null 2>&1 && verb=update-schedule
    aws scheduler $verb --name "$1" --schedule-expression "$2" --schedule-expression-timezone Asia/Kolkata \
        --flexible-time-window '{"Mode":"OFF"}' --target "$target" --state ENABLED >/dev/null
    echo "$1: $2"
}
schedule $NAME-start "cron(0/15 8-9 ? * MON-FRI *)" '{"action":"start"}'
schedule $NAME-check "cron(45 8 ? * MON-FRI *)" '{"action":"check"}'
schedule $NAME-stop "cron(0 16 ? * MON-FRI *)" "{\"InstanceIds\":[\"$INSTANCE\"]}" arn:aws:scheduler:::aws-sdk:ec2:stopInstances
echo "installed $FN_ARN"
