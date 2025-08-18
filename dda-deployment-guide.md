# DDA Deployment Guide

## Step 1: Build Server Setup

1. **Launch an EC2 instance**
   - Use Ubuntu 24.04 as the base AMI
   - Ensure the instance has at least 4GB RAM and 64GB storage (t2.medium or higher)


2. **Clone and build the DDA application**
   ```bash
   # Clone the repository
   git clone https://github.com/your-repo/DDA-OpenSource.git
   cd DDA-OpenSource

   #copy using rsync


3. **Connect to your build server**
   ```bash
   ssh -i "your-key.pem" ubuntu@your-build-server-ip
   ```


4. **Setup the environment 

   # Set up the build server environment
   ./setup-build-server.sh

   # Build the component
   gdk component build

   # Publish the component. If you have permission issue, provide the ec2 instance permission to access the S3 bucket. 
   gdk component publish
   ```



## Step 2: Edge Device Setup

1. **Launch an EC2 instance for the edge device**
   - Use Ubuntu 24.04 as the base AMI
   - Ensure the instance has at least 4GB RAM and 20GB storage

2. **Connect to your edge device**
   ```bash
   ssh -i "your-key.pem" ubuntu@your-edge-device-ip
   ```

3. **Install AWS IoT Greengrass Core**
   3. **Install AWS IoT Greengrass Core**

   ```bash
   # Copy the installGreengrassCore.sh script to the edge device
   scp -i "your-key.pem" installGreengrassCore.sh ubuntu@your-edge-device-ip:~/

   # Make the script executable
   chmod +x installGreengrassCore.sh

   # Run the installer script
   sudo -E ./installGreengrassCore.sh

   ```

4. **Update IAM permissions for S3 access for both build server and edge device**
   ```bash
   # Create a policy document
   cat > s3-access-policy.json << EOF
   {
       "Version": "2012-10-17",
       "Statement": [
           {
               "Effect": "Allow",
               "Action": [
                   "s3:GetObject",
                   "s3:ListBucket"
               ],
               "Resource": [
                   "arn:aws:s3:::your-component-bucket",
                   "arn:aws:s3:::your-component-bucket/*"
               ]
           }
       ]
   }
   EOF

   # Create and attach the policy (from your local machine or build server)
   aws iam create-policy --policy-name GreengrassS3Access --policy-document file://s3-access-policy.json
   aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/GreengrassS3Access
   ```

## Step 3: Deploy the Component

1. **Deploy from AWS CLI**
   ```bash
   # From your local machine or build server
   aws greengrassv2 create-deployment \
     --target-arn "arn:aws:iot:us-east-1:$(aws sts get-caller-identity --query Account --output text):thing/YourThingName" \
     --components '{
       "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
       "aws.edgeml.dda.LocalServer": {"componentVersion": "1.0.3"}
     }' \
     --deployment-name "DDA-Deployment" \
     --region us-east-1
   ```

2. **Monitor the deployment**
   ```bash
   # On the edge device
   sudo tail -f /aws_dda/greengrass/v2/logs/greengrass.log
   ```

3. **Access the DDA application**
   ```bash
   # Set up SSH port forwarding
   ssh -i "your-key.pem" -L 3000:localhost:3000 -L 5000:localhost:5000 ubuntu@your-edge-device-ip
   ```

   Then open http://localhost:3000 in your browser.

## Troubleshooting

- **Docker permission issues**: Run `sudo chmod 666 /var/run/docker.sock`
- **S3 access denied**: Check IAM permissions for the Greengrass role
- **Component not found**: Verify the component was published correctly
- **Deployment fails**: Check logs at `/aws_dda/greengrass/v2/logs/greengrass.log`
- **Missing backend container**: Check component logs with `sudo tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log` and manually start containers with `cd /aws_dda/greengrass/v2/packages/artifacts/aws.edgeml.dda.LocalServer/<version>/ && sudo docker-compose up -d`