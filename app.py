import argparse
import os
import time
from threading import Thread

import boto3
from flask import Flask, request

# Usage
# . venv/bin/activate
# pip install -r requirements.txt
# python app.py
# or launch.sh


# As ngrok has a free tier, you can use it to expose the callback URL, however,
# you might find the rate limit to be too low. In that case, you can use a
# SSH tunnel instead:
# /etc/ssh/sshd_config: GatewayPorts yes
# ssh -R 0.0.0.0:5000:localhost:5000 <some-public-instance>
# and set the callback URL to http://<some-public-instance>:5000/callback
#
CALLBACK_URL = "http://<IP>>:5000/callback"  # Should route back to this script
REGION = "eu-west-1"
SECURITY_GROUP_ID = "<sg-id>"
SUBNET_ID = "<subnet-id>"
VPC_ID = "<vpc-id>"
SSH_KEY_NAME = None


# Create an ArgumentParser object
parser = argparse.ArgumentParser(description="A sample script with named arguments")

# Add named arguments
parser.add_argument("--count", help="Number of instances to launch", default=2, type=int)
parser.add_argument("--type", help="Type of instances to launch", default="t3a.2xlarge", type=str)
parser.add_argument("--ebs-size", help="Size of EBS volume to attach", default=8, type=int)
parser.add_argument("--ami", help="AMI to launch", default="ami-01dd271720c1ba44f", type=str)
parser.add_argument("--ebs-type", help="Volume Type", default="gp2", type=str)
parser.add_argument("--ebs-encrypted", help="Volume Encrypted", default=False, action='store_true')
parser.add_argument("--ebs-iops", help="Volume IOPS", default=None, type=int)
parser.add_argument("--ebs-throughput", help="Volume Throughput", default=None, type=int)


# Parse the command-line arguments
args = parser.parse_args()
INSTANCE_TYPE = args.type
EBS_VOLUME_SIZE = args.ebs_size
AMI_ID = args.ami
INSTANCE_COUNT = args.count

# Volume Options
VOLUME_TYPE = args.ebs_type
VOLUME_ENCRYPTED = args.ebs_encrypted
VOLUME_IOPS = args.ebs_iops
VOLUME_THROUGHPUT = args.ebs_throughput
HIBERNATION_ENABLED = False

# These variables are used for shared state tracking
INSTANCES = []
INSTANCES_SECS = {}
UNIX_START = -1
OUTPUT_FILE = "output-{}.csv".format(INSTANCE_TYPE)

# Setup Boto3
ec2 = boto3.client('ec2', region_name=REGION)

# Prepare CSV Header if not exists
if not os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, "a") as logger:
        logger.write("TEST AT {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
        logger.write(
            "Phase, Instance ID, Instance Type, Volume Size, Volume Type, Volume Enc, AMI, Boot Duration, OS Uptime\n")


def main():
    print("=====================================")
    print("Running main")
    print("Instance type: {}".format(INSTANCE_TYPE))
    print("Instance count: {}".format(INSTANCE_COUNT))
    print("AMI ID: {}".format(AMI_ID))
    print("EBS volume size: {}".format(EBS_VOLUME_SIZE))
    print("EBS volume type: {}".format(VOLUME_TYPE))
    print("EBS volume encrypted: {}".format(VOLUME_ENCRYPTED))
    print("Hibernation enabled: {}".format(HIBERNATION_ENABLED))
    print("Output file: {}".format(OUTPUT_FILE))
    print("=====================================")

    # Launch the instances and time how long it takes
    launch_instances()
    wait_and_print_results("launch_instance")

    #terminate_instances()
    #os._exit(1)  # Force terminate python - dirty but works

    stop_instances()

    # Start the instances and time how long it takes
    start_instances()
    wait_and_print_results("start_instance")

    #terminate_instances()
    #os._exit(1)  # Force terminate python - dirty but works

    # Hibernate the instances
    #hibernate_instances()

    # Start the instances and time how long it takes
    #start_instances()
    #wait_and_print_results("start_from_hibernate")

    # Terminate the instances
    #terminate_instances()

    exit(0)


def wait_and_print_results(phase):
    # Wait until all have called home
    delay = 10
    count = 0
    while len(INSTANCES_SECS) < INSTANCE_COUNT:
        print(
            "Waited {} secs for {} instances to call home".format(delay * count, INSTANCE_COUNT - len(INSTANCES_SECS)))
        time.sleep(delay)
        count += 1

    with open(OUTPUT_FILE, "a") as logger:
        for instance_id, item in INSTANCES_SECS.items():
            wait, proc_uptime = item['wait'], item['proc_uptime']
            enc = "encrypted" if VOLUME_ENCRYPTED else "unencrypted"
            volume_type = VOLUME_TYPE
            if VOLUME_IOPS is not None:
                volume_type += " - {} IOPS".format(VOLUME_IOPS)
            if VOLUME_THROUGHPUT is not None:
                volume_type += " - {} MB/s".format(VOLUME_THROUGHPUT)

            # Output as CSV (TODO: Change to a CSV library for future improvements)
            buffer = "{}, {}, {}, {}, {}, {}, {}, {}, {}\n".format(phase, instance_id, INSTANCE_TYPE, EBS_VOLUME_SIZE,
                                                                   volume_type, enc, AMI_ID, wait,
                                                                   proc_uptime)
            logger.write(buffer)
            print(buffer)


def get_ami_device_path():
    # Get the device path of the root volume
    response = ec2.describe_images(ImageIds=[AMI_ID])
    return response['Images'][0]['RootDeviceName']


def launch_instances():
    global UNIX_START
    UNIX_START = time.time()

    # Always run the script, even on reboots
    userdata_script = """Content-Type: multipart/mixed; boundary="//"
MIME-Version: 1.0

--//
Content-Type: text/cloud-config; charset="us-ascii"
MIME-Version: 1.0
Content-Transfer-Encoding: 7bit
Content-Disposition: attachment; filename="cloud-config.txt"

#cloud-config
cloud_final_modules:
- [scripts-user, always]

--//
Content-Type: text/x-shellscript; charset="us-ascii"
MIME-Version: 1.0
Content-Transfer-Encoding: 7bit
Content-Disposition: attachment; filename="userdata.txt"
#!/bin/bash

cat >> /call_home.sh << 'EOF'
#!/bin/bash

# If top is not already running, launch it and record the pid
if ! pgrep -x "top" > /dev/null
then
    top -b -d 0.2 > /dev/null &
    sleep 0.1
    echo "top launched"
fi

PID=$(pgrep -x "top")

INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
UPTIME=$(awk '{{print $1}}' /proc/uptime)
DISK=$(df -h / | awk '{{print $4}}' | tail -n 1)
TYPE=$1
curl -X GET "{callback_url}?instance_id=$INSTANCE_ID&type=$TYPE&uptime=$UPTIME&pid=$PID&disk_free=$DISK"

EOF

chmod +x /call_home.sh

bash /call_home.sh startup
watch -n0.2 'bash /call_home.sh watch' &
""".format(callback_url=CALLBACK_URL)
    response = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        MaxCount=INSTANCE_COUNT,
        MinCount=INSTANCE_COUNT,
        KeyName=SSH_KEY_NAME,
        EbsOptimized=True,
        HibernationOptions={
            'Configured': HIBERNATION_ENABLED
        },
        BlockDeviceMappings=[
            {
                'DeviceName': get_ami_device_path(),
                'Ebs': {
                    'DeleteOnTermination': True,
                    'VolumeSize': EBS_VOLUME_SIZE,
                    'VolumeType': VOLUME_TYPE,
                    **({'Iops': VOLUME_IOPS} if VOLUME_IOPS is not None else {}),
                    **({'Throughput': VOLUME_THROUGHPUT} if VOLUME_THROUGHPUT is not None else {}),
                    'Encrypted': VOLUME_ENCRYPTED,
                },
            },
        ],
        NetworkInterfaces=[
            {
                'AssociatePublicIpAddress': True,
                'DeleteOnTermination': True,
                'DeviceIndex': 0,
                'SubnetId': SUBNET_ID,
                'Groups': [
                    SECURITY_GROUP_ID,
                ],
            },
        ],
        UserData=userdata_script,
        MetadataOptions={
            'HttpTokens': 'optional',
            'HttpEndpoint': 'enabled'
        },
        TagSpecifications=[{
            'ResourceType': 'instance',
            'Tags': [
            {
                'Key': 'Name',
                'Value': 'LT-{}-{}-{}'.format(VOLUME_TYPE, EBS_VOLUME_SIZE, AMI_ID)
            },
        ]
        }]
    )

    # Launch the instancess
    print("Calling launch_instances")

    # Save the instance IDs
    for instance in response['Instances']:
        INSTANCES.append(instance['InstanceId'])


def terminate_instances():
    print("Calling terminate_instances")
    ec2.terminate_instances(InstanceIds=INSTANCES)


def stop_instances(wait=True):
    print("Calling stop_instances")
    ec2.stop_instances(InstanceIds=INSTANCES)
    if wait:
        # Wait for the instances to stop
        waiter = ec2.get_waiter('instance_stopped')
        waiter.wait(InstanceIds=INSTANCES)
    # No more requests should be seen
    INSTANCES_SECS.clear()


def hibernate_instances():
    print("Calling hibernate_instances")
    while True:
        try:
            ec2.stop_instances(InstanceIds=INSTANCES, Hibernate=True)
            break
        except Exception as e:
            print("Exception while hibernating instances, retrying in 2 seconds: {}".format(e))
            time.sleep(2)
            continue
    # Wait for the instances to stop
    waiter = ec2.get_waiter('instance_stopped')
    waiter.wait(InstanceIds=INSTANCES)
    # No more requests should be seen
    INSTANCES_SECS.clear()


def start_instances():
    global UNIX_START
    print("Calling start_instances")
    UNIX_START = time.time()
    ec2.start_instances(InstanceIds=INSTANCES)


# Callback function
app = Flask(__name__)


@app.route('/callback')
def callback():
    instance_id = request.args.get('instance_id')
    proc_uptime = request.args.get('uptime')
    proc_pid = request.args.get('pid')

    if instance_id not in INSTANCES:
        print("IGNORING - Instance {} not in INSTANCES".format(instance_id))
        return "REJECTED!"

    # Only record the first callback
    if instance_id not in INSTANCES_SECS:
        INSTANCES_SECS[instance_id] = {
            'wait': round(time.time() - UNIX_START, 2),
            'proc_uptime': proc_uptime,
            'proc_pid': proc_pid,
        }
        print("Instance callback, took {} seconds".format(INSTANCES_SECS[instance_id]))
    return 'OK!'


# Run main while also running the Flask app
if __name__ == '__main__':
    p = Thread(target=main)
    p.start()
    app.run(debug=True, use_reloader=False)
    p.join()
