# Defect Detection Application System (DDA)

**This system is an edge deployed solution for quality assurance in discrete manufacturing. It normally deploys in AWS IoT Greengrass and is a former product of the AWS EdgeML service team. This system is being brought under open source under the stewardship of the AWS Manufacturing TFC and the Auto/Manufacturing IBU, as well as a V-Team of builders led by @ryvan**

## Documentation

https://docs.aws.amazon.com/lookout-for-vision/latest/dda-user-guide/what-is.html

## Supported Device Types

DDA supports the following device types:
1. X86_64 CPU - Standard x86_64 architecture
2. Jetson Xavier JetPack4 - ARM64 architecture with NVIDIA Jetson-specific hardware
3. ARM64 CPU - Standard ARM64 architecture

## Building and Deploying with EC2 on AWS

### Prerequisites

- AWS Account with appropriate permissions
- Basic knowledge of AWS services (EC2, S3, IAM)
- AWS CLI installed and configured

### Step 1: Launch an EC2 Instance

1. Launch an EC2 instance with the appropriate architecture:
   - For x86_64 builds: Use an x86_64 instance type (e.g., t2.large)
   - For ARM64 builds: Use an ARM64 instance type (e.g., t4g.large)

2. Use Ubuntu 20.04 LTS as the base AMI

3. Ensure the instance has at least 4GB RAM and 20GB storage

4. Configure security group to allow SSH access (port 22)

### Step 2: Set Up the Build Environment

1. Connect to your EC2 instance:
   ```bash
   ssh -i your-key.pem ubuntu@your-ec2-instance-ip
   ```

2. Install dependencies:
   ```bash
   sudo apt-get update
   sudo apt-get install -y git docker.io docker-compose python3.9 python3.9-venv python3-pip
   sudo usermod -aG docker $USER
   # Log out and log back in for group changes to take effect
   ```

3. Install the Greengrass Development Kit (GDK):
   ```bash
   pip3 install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git
   ```

### Step 3: Clone and Build the DDA Application

1. Clone the repository:
   ```bash
   git clone https://github.com/your-org/DDA-OpenSource.git
   cd DDA-OpenSource
   ```

2. Configure your AWS credentials:
   ```bash
   aws configure
   ```

3. Update the `gdk-config.json` file with your S3 bucket and region:
   ```json
   {
     "component": {
       "aws.edgeml.dda.LocalServer": {
         "author": "Amazon",
         "version": "NEXT_PATCH",
         "build": {
           "build_system": "custom",
           "custom_build_command": [
             "bash",
             "build-custom.sh",
             "aws.edgeml.dda.LocalServer",
             "NEXT_PATCH"
           ]
         },
         "publish": {
           "bucket": "YOUR-S3-BUCKET-NAME",
           "region": "YOUR-AWS-REGION"
         }
       }
     },
     "gdk_version": "1.0.0"
   }
   ```

4. Build the component:
   ```bash
   gdk component build
   ```

5. Publish the component to AWS IoT Greengrass:
   ```bash
   gdk component publish
   ```

### Step 4: Deploy to an Edge Device

1. Set up your edge device with AWS IoT Greengrass v2 using the provided script:
   ```bash
   sudo ./installGreengrassCore.sh
   ```
   Note: You'll need to modify the script with your specific AWS region, thing name, and thing group.

2. Deploy the DDA component to your device using the AWS IoT Greengrass console or AWS CLI:
   ```bash
   aws greengrassv2 create-deployment \
     --target-arn "arn:aws:iot:region:account-id:thing/thing-name" \
     --components '{"aws.edgeml.dda.LocalServer":{"componentVersion":"1.0.0"}}' \
     --deployment-name "DDA-Deployment" \
     --region your-region
   ```

3. Monitor the deployment status:
   ```bash
   aws greengrassv2 list-deployments \
     --target-arn "arn:aws:iot:region:account-id:thing/thing-name" \
     --region your-region
   ```

## Development

For detailed development instructions, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Testing with Static Images on EC2

### Setting Up the Test Environment

1. Create a test directory on your EC2 instance:
   ```bash
   mkdir -p /aws_dda/image-capture/test-images
   mkdir -p /aws_dda/inference-results
   ```

2. Upload test images to your EC2 instance:
   ```bash
   # From your local machine
   scp -i your-key.pem your-test-images/* ubuntu@your-ec2-instance-ip:/aws_dda/image-capture/test-images/
   ```

3. Set appropriate permissions:
   ```bash
   sudo chown -R dda_admin_user:dda_admin_group /aws_dda/image-capture
   sudo chown -R dda_admin_user:dda_admin_group /aws_dda/inference-results
   sudo chmod -R 770 /aws_dda/image-capture
   sudo chmod -R 770 /aws_dda/inference-results
   ```

### Running Local Tests

1. Start the DDA application in local mode:
   ```bash
   cd DDA-OpenSource/src
   docker-compose --profile generic up
   ```

2. Access the DDA web interface:
   ```bash
   # Configure port forwarding if accessing remotely
   ssh -i your-key.pem -L 3000:localhost:3000 ubuntu@your-ec2-instance-ip
   ```
   Then open a browser and navigate to `http://localhost:3000`

3. Process static images through the web interface:
   - Navigate to the "Images" section
   - Select images from the `/aws_dda/image-capture/test-images` directory
   - Run inference on the selected images

4. Alternatively, use the API to process images:
   ```bash
   curl -X POST http://localhost:5000/api/v1/inference \
     -H "Content-Type: application/json" \
     -d '{"imagePath": "/aws_dda/image-capture/test-images/your-image.jpg"}'  
   ```

5. View results in the `/aws_dda/inference-results` directory or through the web interface.

## Updating and Redeploying Components

### Updating the Codebase

1. Pull the latest changes from the repository:
   ```bash
   cd DDA-OpenSource
   git pull origin main
   ```

2. Make your code changes to the relevant files.

3. Test your changes locally if possible.

### Incrementing Component Version

When making changes to the component, you need to increment the version number:

1. Update the version in `gdk-config.json`:
   ```json
   {
     "component": {
       "aws.edgeml.dda.LocalServer": {
         "version": "1.0.1",  # Increment this version number
         ...
       }
     }
   }
   ```

2. Alternatively, keep using `"NEXT_PATCH"` to automatically increment the patch version.

### Rebuilding and Redeploying

1. Rebuild the component:
   ```bash
   gdk component build
   ```

2. Publish the updated component:
   ```bash
   gdk component publish
   ```

3. Create a new deployment with the updated component version:
   ```bash
   aws greengrassv2 create-deployment \
     --target-arn "arn:aws:iot:region:account-id:thing/thing-name" \
     --components '{"aws.edgeml.dda.LocalServer":{"componentVersion":"1.0.1"}}' \
     --deployment-name "DDA-Deployment-Update" \
     --region your-region
   ```

4. Monitor the deployment status:
   ```bash
   aws greengrassv2 list-deployments \
     --target-arn "arn:aws:iot:region:account-id:thing/thing-name" \
     --region your-region
   ```

## License

Copyright [2025] [Amazon Web Services, Inc.]

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
