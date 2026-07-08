import boto3
import time
import argparse
import argparse
import os
import json
import re
import shlex
from botocore.exceptions import ClientError

# Allowlist patterns for argparse args that are interpolated into the
# AWS-RunShellScript SSM commands. Any value carrying a shell metacharacter
# (; | & ` $ ( ) < > whitespace, ...) fails these patterns and is rejected
# before it can reach the shell. shlex.quote (applied at command-build time) is
# additionally a no-op on these clean tokens, so legitimate args are unchanged.
_ALLOWED_ARG = re.compile(r"^[A-Za-z0-9._:-]+$")
_ALLOWED_RELEASE_DATE = re.compile(r"^\d{8}$")


def _validate_deploy_args(args):
    """Allowlist-validate the argparse args as they are read, before any value is
    interpolated into an SSM shell command. Rejects on mismatch with a
    ``ValueError`` so a metacharacter-bearing arg never reaches AWS-RunShellScript.
    Only fields that are present on ``args`` are checked (the mqtt-only args
    ``region``/``mqtt_endpoint``/``payload_size`` are absent unless the ``mqtt``
    subcommand is used)."""
    token_fields = [
        "platform",
        "ubuntu_version",
        "python_version",
        "region",
        "mqtt_endpoint",
    ]
    for field in token_fields:
        if hasattr(args, field) and getattr(args, field) is not None:
            value = getattr(args, field)
            if not _ALLOWED_ARG.match(str(value)):
                raise ValueError(
                    f"Invalid value for --{field}: {value!r} "
                    f"(must match {_ALLOWED_ARG.pattern})"
                )
    if hasattr(args, "release_date") and args.release_date is not None:
        if not _ALLOWED_RELEASE_DATE.match(str(args.release_date)):
            raise ValueError(
                f"Invalid value for --release_date: {args.release_date!r} "
                f"(must be an 8-digit date YYYYMMDD)"
            )
    for field in ["longevity_hours", "payload_size"]:
        if hasattr(args, field) and getattr(args, field) is not None:
            value = getattr(args, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Invalid value for --{field}: {value!r} (must be an int)"
                )
    return args


aws_region = ""
secret_name = "edgeml-sdk-longevity-tests"
# EC2 instance parameters
ec2_instance_type = 't4g.2xlarge'
ec2_image_id = 'ami-073614ec1eee63d19' # Amazon Linux 2023 AMI 2023.1.20230912.0 arm64 HVM kernel-6.1

iam_instance_profile_arn =  'arn:aws:iam::ACCOUNT_ID:instance-profile/iam_role_for_edgemlsdk_longevity_tests'
 
class DeployLongevity:
    def __init__(self, instance_type="ec2"):
        self.instance_type = instance_type

    def create_instance(self, credentials, iam_instance_profile_arn):
        if self.instance_type == "ec2":
            self.ec2_client = boto3.client(
                "ec2",
                aws_access_key_id=credentials.access_key,
                aws_secret_access_key=credentials.secret_key,
                aws_session_token=credentials.token,
                region_name=aws_region,
            )

            response = self.ec2_client.run_instances(
                ImageId=ec2_image_id,
                InstanceType=ec2_instance_type,
                IamInstanceProfile={"Arn": iam_instance_profile_arn},
                MinCount=1,
                MaxCount=1,
                UserData="""sudo yum update -y""",
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [{"Key": "Name", "Value": "edgemlsdk-longevity"}],
                    }
                ],
                MetadataOptions={
                    "HttpTokens": "required",
                    "HttpEndpoint": "enabled",
                },
                BlockDeviceMappings=[
                    {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100}}
                ],
            )

            instance_id = response["Instances"][0]["InstanceId"]
            self.ec2_client.get_waiter("instance_running").wait(
                InstanceIds=[instance_id]
            )
            time.sleep(30)
            return instance_id


    def run_commands_via_ssm_with_retry(
        self, instance_id, max_retries, commands, credentials
    ):
        retries = 0
        # Create SSM client
        self.ssm_client = boto3.client(
            "ssm",
            aws_access_key_id=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_session_token=credentials.token,
            region_name=aws_region,
        )

        while retries < max_retries:
            # Send the commands to the instance
            response = self.ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": commands},
                MaxConcurrency="50",
                MaxErrors="0",
            )

            command_id = response["Command"]["CommandId"]

            # Wait for the command to finish
            while True:
                time.sleep(5)
                output = self.ssm_client.get_command_invocation(
                    InstanceId=instance_id, CommandId=command_id
                )
                print(output["Status"])
                if output["Status"] in ["Success", "Failed", "Cancelled"]:
                    break

            # Get the output of the command
            command_output = output["StandardOutputContent"]
            error_output = output["StandardErrorContent"]

            print(f"Command output:{command_output}")
            if error_output != "":
                print(f"Error/Warning output: {error_output}")
                retries +=1
            else: return
 
    def close_ssm(self):
        self.ssm_client.close()
 
# Function to upload files from a local folder to S3
def upload_folder_to_s3(s3, source_folder, bucket_name, destination_prefix):
    for root, dirs, files in os.walk(source_folder):
        for file in files:
            local_path = os.path.join(root, file)
            s3_path = os.path.join(destination_prefix, os.path.relpath(local_path, source_folder))
            s3.upload_file(local_path, bucket_name, s3_path)

# function to upload single file to s3 bucket
def upload_file_to_s3(s3_client, file_path, bucket_name, destination_path='/'):
    s3_client.upload_file(file_path, bucket_name, destination_path)

def set_aws_access_keys_from_secrets_manager():
    
    session = boto3.Session()
    secrets_manager_client = session.client(
        service_name='secretsmanager',
        region_name=session.region_name
    )
    try:
        get_secret_value_response = secrets_manager_client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        # For a list of exceptions thrown, see
        # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
        raise e

    # Decrypts secret using the associated KMS key.
    secrets = json.loads(get_secret_value_response['SecretString'])
    AWS_ACCESS_KEY_ID = secrets["AWS_ACCESS_KEY_ID"]
    AWS_SECRET_ACCESS_KEY = secrets["AWS_SECRET_ACCESS_KEY"]
    os.environ["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
    os.environ["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    os.environ["AWS_SESSION_TOKEN"]=""
    
def main(args):
    _validate_deploy_args(args)
    set_aws_access_keys_from_secrets_manager()
    session = boto3.Session()
    global aws_region
    aws_region = session.region_name
    credentials = session.get_credentials()
    s3_client = session.client('s3', region_name=aws_region)
    source_folder = ''
    destination_prefix = ''
    if args.mqtt:
        source_folder = 'mqtt/'
        destination_prefix = 'mqtt'
    bucket_name = 'edgeml-sdk-longevity-tests'
    
    upload_folder_to_s3(s3_client, source_folder, bucket_name, destination_prefix)
    graph_json_path = os.path.join(os.getcwd(), 'longevity.json')
    delegates_json_path = os.path.join(os.getcwd(),'delegates.json')
    upload_file_to_s3(s3_client,graph_json_path, bucket_name, "longevity.json")
    upload_file_to_s3(s3_client,delegates_json_path, bucket_name, "delegates.json")
    
    # Allowlist-validated above; additionally shlex.quote every interpolated arg
    # so no shell metacharacter can survive into the AWS-RunShellScript command.
    # shlex.quote is a no-op on the clean allowlisted tokens, so legitimate args
    # produce byte-identical command strings.
    q_region = shlex.quote(str(args.region))
    q_release_date = shlex.quote(str(args.release_date))
    q_platform = shlex.quote(str(args.platform))
    q_ubuntu_version = shlex.quote(str(args.ubuntu_version))
    q_python_version = shlex.quote(str(args.python_version))

    download_edgemlsdk_release_artifacts = [
        "sudo yum update",
        "sudo yum install docker -y",
        "sudo service docker start",
        "sudo service docker status",
        f"export AWS_ACCESS_KEY_ID={credentials.access_key}",
        f"export AWS_SECRET_ACCESS_KEY={credentials.secret_key}",
        f"export AWS_DEFAULT_REGION={q_region}",
        "sudo mkdir -p /edgemlsdk",
        f"sudo mkdir -p /edgemlsdk/{source_folder}",
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{q_release_date}/{q_platform}/{q_ubuntu_version}/3.8.0/ /edgemlsdk",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
        f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}",
        "aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com",
        f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{q_ubuntu_version}-{q_platform}-{q_python_version}-latest",
    ]

    if args.mqtt:
        q_mqtt = shlex.quote(str(args.mqtt))
        q_longevity_hours = shlex.quote(str(args.longevity_hours))
        q_mqtt_endpoint = shlex.quote(str(args.mqtt_endpoint))
        q_payload_size = shlex.quote(str(args.payload_size))
        run_mqtt_longevity = [f"docker run -v /edgemlsdk:/edgemlsdk -idt --log-driver=awslogs --log-opt awslogs-region=us-west-2 --log-opt awslogs-group=edgemlsdk-{q_ubuntu_version}-{q_platform}-{q_python_version}-{q_mqtt} --log-opt awslogs-create-group=true \
            ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{q_ubuntu_version}-{q_platform}-{q_python_version}-latest \
            bash -c '''cd /edgemlsdk; dpkg -i Panorama_1.0.{q_release_date}.deb;apt-get install tmux -y; python3 -m pip install panorama-1.0-py3-none-any.whl; bash /edgemlsdk/mqtt/run_mqtt_longevity.sh -l {q_longevity_hours} -r {q_region} -m {q_mqtt_endpoint} -n {q_payload_size} -a {credentials.access_key} -s {credentials.secret_key}'''"]
    task = DeployLongevity("ec2")
    instance_id = task.create_instance(
        credentials, iam_instance_profile_arn
    )

    # Download edgeml sdk release artifacts
    task.run_commands_via_ssm_with_retry(
        instance_id, 3, download_edgemlsdk_release_artifacts, credentials
    )
    if args.mqtt:
        task.run_commands_via_ssm_with_retry(instance_id, 3, run_mqtt_longevity, credentials)
    
    task.close_ssm()

 
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub_parser = parser.add_subparsers(help="Run Mqtt Longevity Tests", dest="mqtt")
    mqtt_parser = sub_parser.add_parser('mqtt', help='run `python3 deploy.py mqtt -h --help` to see what arguments can be passed')
    parser.add_argument(
        "--platform",
        type=str,
        default="aarch64",
        help="set platform for downloading edgemlsdk artifacts",
    )
    parser.add_argument(
        "--ubuntu_version",
        type=str,
        default="18.04",
        help="ubuntu version for downloading edgemlsdk artifacts",
    )
    parser.add_argument(
        "--python_version",
        type=str,
        default="3.8",
        help="Sets python version for downloading edgemlsdk artifacts",
    )
    parser.add_argument(
        "--release_date",
        type=str,
        default="20230918",
        help="Sets relase date folder(YYYYMMDD) for downloading edgemlsdk artifacts",
    )

    parser.add_argument(
        "--longevity_hours",
        type=int,
        default=72,
        help="run longevity for n hrs",
    )

    mqtt_parser.add_argument(
        "--region",
        type=str,
        default="us-west-2",
        help="aws region of iot endpoint",
    )
    mqtt_parser.add_argument(
        "--mqtt_endpoint",
        type=str,
        default="a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com",
        help="mqtt endpoint to publish",
    )
    mqtt_parser.add_argument(
        "--payload_size",
        type=int,
        default=50,
        help="payload size in KB to publish to IoT Core"
    )

    args = parser.parse_args()
    main(args)