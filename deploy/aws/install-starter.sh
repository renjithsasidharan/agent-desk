#!/bin/bash
# Install or update the morning starter (starter.py) from a Mac with the AWS CLI logged in:
#   bash deploy/aws/install-starter.sh owner@example.com
# Schedules (Asia/Kolkata, Mon-Fri): groww-trader-start every 15 min 08:00-09:45 (no-op once the
# machine is up), groww-trader-check at 08:45 (email if it is still down), groww-trader-stop at 16:00.
set -euo pipefail
EMAIL=${1:?usage: install-starter.sh <alert email>}
REGION=ap-south-1
INSTANCE=i-06ab29ab790f6c8ea
FN=groww-trader-starter
ACCT=$(aws sts get-caller-identity --query Account --output text)
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
aws() { command aws --region "$REGION" "$@"; }

# Alerts: an SNS topic emailing the owner (AWS sends a confirmation link first).
TOPIC=$(aws sns create-topic --name groww-trader-alerts --tags Key=Project,Value=groww-trader --query TopicArn --output text)
if ! aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" --query 'Subscriptions[].Endpoint' --output text | grep -qw "$EMAIL"; then
    aws sns subscribe --topic-arn "$TOPIC" --protocol email --notification-endpoint "$EMAIL" >/dev/null
    echo "confirmation email sent to $EMAIL"
fi

# The function's role: start and resize this one machine, send alerts, write its log.
ROLE=groww-trader-starter
if ! aws iam get-role --role-name $ROLE >/dev/null 2>&1; then
    aws iam create-role --role-name $ROLE --tags Key=Project,Value=groww-trader --assume-role-policy-document \
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
        --environment "$ENV" --zip-file "fileb://$TMP/starter.zip" --tags Project=groww-trader >/dev/null
fi
aws lambda wait function-updated --function-name $FN
aws logs create-log-group --log-group-name /aws/lambda/$FN 2>/dev/null || true
aws logs put-retention-policy --log-group-name /aws/lambda/$FN --retention-in-days 90
FN_ARN=$(aws lambda get-function --function-name $FN --query Configuration.FunctionArn --output text)

# The existing scheduler role may now also run the function.
aws iam put-role-policy --role-name groww-trader-scheduler --policy-name invoke-starter --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$FN_ARN\"}]}"
sleep 10

schedule() {  # name, cron, input
    local target="{\"Arn\":\"$FN_ARN\",\"RoleArn\":\"arn:aws:iam::$ACCT:role/groww-trader-scheduler\",\"Input\":$(printf '%s' "$3" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"RetryPolicy\":{\"MaximumRetryAttempts\":2,\"MaximumEventAgeInSeconds\":600}}"
    local verb=create-schedule
    aws scheduler get-schedule --name "$1" >/dev/null 2>&1 && verb=update-schedule
    aws scheduler $verb --name "$1" --schedule-expression "$2" --schedule-expression-timezone Asia/Kolkata \
        --flexible-time-window '{"Mode":"OFF"}' --target "$target" --state ENABLED >/dev/null
    echo "$1: $2"
}
schedule groww-trader-start "cron(0/15 8-9 ? * MON-FRI *)" '{"action":"start"}'
schedule groww-trader-check "cron(45 8 ? * MON-FRI *)" '{"action":"check"}'
echo "installed $FN_ARN"
