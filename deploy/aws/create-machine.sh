#!/bin/bash
# Create the trading machine, from a computer with the AWS CLI logged in:
#   bash deploy/aws/create-machine.sh [ssh-cidr]      (AWS_REGION, default ap-south-1 = Mumbai)
# Makes: an SSH key (~/.ssh/groww-trader.pem), a security group allowing SSH from ssh-cidr (default:
# this computer's IP; pass 0.0.0.0/0 if your IP changes, login stays key-only), a t4g.medium Ubuntu
# 24.04 arm64 machine with a 20 GB encrypted disk, and an Elastic IP: the static address you
# register with the broker. Everything is tagged Project=groww-trader.
set -euo pipefail
REGION=${AWS_REGION:-ap-south-1}
CIDR=${1:-$(curl -fsS https://checkip.amazonaws.com)/32}
NAME=groww-trader
KEY=~/.ssh/$NAME.pem
aws() { command aws --region "$REGION" "$@"; }
tags() { echo "ResourceType=$1,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=$NAME}]"; }

if aws ec2 describe-instances --filters "Name=tag:Project,Values=$NAME" \
        "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text | grep -q .; then
    echo "a $NAME machine already exists in $REGION"; exit 1
fi
[ ! -e "$KEY" ] || { echo "$KEY already exists; move it away first"; exit 1; }

aws ec2 create-key-pair --key-name $NAME --key-type ed25519 --tag-specifications "$(tags key-pair)" \
    --query KeyMaterial --output text > "$KEY"
chmod 600 "$KEY"

VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --vpc-id "$VPC" --group-name $NAME-ssh --description "SSH to the trading machine" \
    --tag-specifications "$(tags security-group)" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$CIDR,Description=ssh}]" >/dev/null

AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)
I=$(aws ec2 run-instances --image-id "$AMI" --instance-type t4g.medium --key-name $NAME --security-group-ids "$SG" \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled --credit-specification CpuCredits=unlimited \
    --tag-specifications "$(tags instance)" "$(tags volume)" --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$I"

read -r ALLOC IP < <(aws ec2 allocate-address --domain vpc --tag-specifications "$(tags elastic-ip)" \
    --query '[AllocationId,PublicIp]' --output text)
aws ec2 associate-address --instance-id "$I" --allocation-id "$ALLOC" >/dev/null

cat <<EOF
machine:  $I ($REGION)
address:  $IP   <- register this static IP with your broker
ssh:      ssh -i $KEY ubuntu@$IP
next:     bash deploy/aws/install-starter.sh $I <your email>    (see SETUP.md)
EOF
