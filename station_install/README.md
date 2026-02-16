# Edge Device Installation and Setup

This directory contains scripts and configuration files for provisioning and managing edge devices (Greengrass core devices) for the DDA Portal.

## Quick Start

### Prerequisites

- AWS Account with appropriate IAM permissions
- EC2 instance or physical device (ARM64 or x86_64) running Ubuntu 18.04, 20.04, or 22.04
- AWS CLI configured with credentials
- SSH access to the device

### 1. Launch an Edge Device (EC2)

```bash
./launch-edge-device.sh <region> <device-name>
```

Example:
```bash
./launch-edge-device.sh us-east-1 my-defect-detector
```

This script:
- Creates an EC2 instance with appropriate security groups
- Generates SSH key pair
- Outputs connection details

### 2. Provision the Device

SSH into the device and run the setup script:

```bash
ssh -i "your-key.pem" ubuntu@device-ip

# Run setup (requires sudo)
sudo -E /path/to/setup_station.sh <region> <thing-name>
```

Example:
```bash
sudo ./setup_station.sh us-east-1 my-defect-detector
```

This script:
- Installs system dependencies (Java, Python 3.9, Docker, GStreamer)
- Installs AWS Greengrass Core
- Provisions the device as an IoT Thing
- Configures IAM roles and policies
- Sets up directory structure for inference and image capture

### 3. Deploy Models to Device

From the DDA Portal:
1. Go to **Devices** page
2. Select your device
3. Go to **Deployments** tab
4. Create a new deployment with:
   - `aws.edgeml.dda.LocalServer.*` (required first)
   - Your trained model components
5. Monitor deployment status


## Configuration Files

### edge_manager_agent_config.json
Configuration for AWS Edge Manager Agent. Copied to device during setup.

### edge-device-iam-policy.json
IAM policy document defining permissions for edge devices.

## Directory Structure on Device

After setup, the device will have:

```
/aws_dda/
├── greengrass/v2/              # Greengrass installation
│   ├── config/
│   ├── logs/
│   ├── packages/
│   └── ...
├── image-capture/              # Captured images from camera
│   ├── cookie-dataset/
│   │   ├── test-images/
│   │   ├── alien-normal/
│   │   └── alien-anomaly/
│   └── ...
├── inference-results/          # Inference results (uploaded to S3)
├── em_agent/                   # Edge Manager Agent
└── check-cloudwatch-logging.sh # Diagnostics script
```

## Common Tasks

### SSH into Device

```bash
ssh -i "device-key.pem" ubuntu@device-ip
```

### Check Greengrass Status

```bash
sudo systemctl status greengrass
```

### View Greengrass Logs

```bash
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log
```

### Restart Greengrass

```bash
sudo systemctl restart greengrass
```

### Check Device Deployments

```bash
aws greengrassv2 list-effective-deployments \
  --core-device-thing-name your-device-name \
  --region us-east-1
```

### Test Inference Endpoint

```bash
# Classification
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-class"

# Segmentation
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-segmentation"
```

## Testing Without a Camera (Folder Mode)

For testing and development, you can run inference on pre-captured images without needing a live camera feed. This is useful for:
- Testing model deployments
- Validating inference pipelines
- Benchmarking performance
- Demos and POCs

### Copy Test Images to Device

**From your local machine:**

```bash
# Copy cookie dataset test images
scp -i "device-key.pem" -r datasets/cookie-dataset/test-images/* \
  ubuntu@device-ip:/aws_dda/image-capture/

# Or copy alien dataset images
scp -i "device-key.pem" -r datasets/alien-dataset/normal/* \
  ubuntu@device-ip:/aws_dda/image-capture/alien-normal/

scp -i "device-key.pem" -r datasets/alien-dataset/anomaly/* \
  ubuntu@device-ip:/aws_dda/image-capture/alien-anomaly/
```

**Or from the device, if you have the datasets locally:**

```bash
# Copy from local datasets directory
cp -r /path/to/datasets/cookie-dataset/test-images/* /aws_dda/image-capture/

# Or create subdirectories for organization
mkdir -p /aws_dda/image-capture/cookie-dataset/test-images
cp /path/to/datasets/cookie-dataset/test-images/* /aws_dda/image-capture/cookie-dataset/test-images/
```

### Run Inference on Folder Images

**Test with individual images:**

```bash
# List available test images
ls -la /aws_dda/image-capture/

# Run inference on a single image
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-normal-1.jpg" \
  -F "model_name=model-cookie-class"

# Test with anomaly image
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-anomaly-1.jpg" \
  -F "model_name=model-cookie-segmentation"
```

**Batch test all images in a folder:**

```bash
#!/bin/bash
# Script to test all images in a folder

IMAGE_DIR="/aws_dda/image-capture"
MODEL_NAME="model-cookie-class"
RESULTS_DIR="/tmp/inference-results"

mkdir -p $RESULTS_DIR

for image in $IMAGE_DIR/*.jpg; do
  if [ -f "$image" ]; then
    echo "Testing: $(basename $image)"
    
    # Run inference and save result
    curl -X POST http://localhost:5000/api/v1/inference \
      -F "image=@$image" \
      -F "model_name=$MODEL_NAME" \
      > "$RESULTS_DIR/$(basename $image).json"
    
    # Print result
    cat "$RESULTS_DIR/$(basename $image).json" | python3 -m json.tool
    echo "---"
  fi
done

echo "Results saved to: $RESULTS_DIR"
```

**Test multiple models:**

```bash
# Test classification model
for img in /aws_dda/image-capture/test-*.jpg; do
  echo "Classification: $(basename $img)"
  curl -s -X POST http://localhost:5000/api/v1/inference \
    -F "image=@$img" \
    -F "model_name=model-cookie-class" | python3 -m json.tool | grep "Inference result"
done

# Test segmentation model
for img in /aws_dda/image-capture/test-*.jpg; do
  echo "Segmentation: $(basename $img)"
  curl -s -X POST http://localhost:5000/api/v1/inference \
    -F "image=@$img" \
    -F "model_name=model-cookie-segmentation" | python3 -m json.tool | grep "Inference result"
done
```

### Configure Folder Mode in LocalServer

To automatically process images from a folder without a camera:

**1. SSH into the device:**
```bash
ssh -i "device-key.pem" ubuntu@device-ip
```

**2. Edit the LocalServer configuration:**
```bash
# Find the LocalServer config file
find /aws_dda/greengrass/v2 -name "config.json" -path "*/LocalServer/*"

# Edit the configuration
sudo nano /aws_dda/greengrass/v2/packages/artifacts/aws.edgeml.dda.LocalServer/config.json
```

**3. Set folder mode instead of camera:**
```json
{
  "input_source": "folder",
  "input_folder": "/aws_dda/image-capture",
  "image_extensions": [".jpg", ".jpeg", ".png"],
  "process_interval_seconds": 5,
  "models": [
    {
      "name": "model-cookie-class",
      "enabled": true
    },
    {
      "name": "model-cookie-segmentation",
      "enabled": true
    }
  ]
}
```

**4. Restart LocalServer:**
```bash
sudo systemctl restart greengrass
```

### Monitor Folder Mode Processing

```bash
# Watch for new inference results
watch -n 1 'ls -lah /aws_dda/inference-results/'

# Monitor LocalServer logs for folder processing
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log | grep -i "folder\|processing"

# Check inference results
cat /aws_dda/inference-results/*/latest.jsonl | python3 -m json.tool
```

### Organize Test Images by Category

For better testing, organize images by type:

```bash
# Create directory structure
mkdir -p /aws_dda/image-capture/{normal,anomaly,test}

# Copy images to appropriate folders
cp /path/to/normal-images/* /aws_dda/image-capture/normal/
cp /path/to/anomaly-images/* /aws_dda/image-capture/anomaly/
cp /path/to/test-images/* /aws_dda/image-capture/test/

# List organized images
tree /aws_dda/image-capture/
```

### Performance Testing with Folder Mode

```bash
#!/bin/bash
# Benchmark inference performance on folder of images

IMAGE_DIR="/aws_dda/image-capture"
MODEL_NAME="model-cookie-class"
ITERATIONS=10

echo "Performance Test: $MODEL_NAME"
echo "Images: $(ls $IMAGE_DIR/*.jpg | wc -l)"
echo "Iterations: $ITERATIONS"
echo ""

total_time=0

for i in $(seq 1 $ITERATIONS); do
  start_time=$(date +%s%N)
  
  for image in $IMAGE_DIR/*.jpg; do
    curl -s -X POST http://localhost:5000/api/v1/inference \
      -F "image=@$image" \
      -F "model_name=$MODEL_NAME" > /dev/null
  done
  
  end_time=$(date +%s%N)
  elapsed=$((($end_time - $start_time) / 1000000))  # Convert to ms
  total_time=$(($total_time + $elapsed))
  
  echo "Iteration $i: ${elapsed}ms"
done

avg_time=$(($total_time / $ITERATIONS))
echo ""
echo "Average time: ${avg_time}ms"
echo "Throughput: $(echo "scale=2; 1000 / $avg_time" | bc) inferences/sec"
```

### Check CloudWatch Logs

```bash
# Run diagnostics script on device
/aws_dda/check-cloudwatch-logging.sh us-east-1
```

## Troubleshooting

### Device Provisioning Fails

**Error:** "Policy does not exist"
```
An error occurred (NoSuchEntity) when calling the AttachRolePolicy operation: 
Policy arn:aws:iam::ACCOUNT_ID:policy/DDAPortalComponentAccessPolicy does not exist
```

**Solution:** Deploy the `DDAPortalUseCaseAccountStack` CDK stack first:
```bash
cd edge-cv-portal/infrastructure
cdk deploy -c environment=production
```

### Greengrass Won't Start

```bash
# Check logs
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log

# Restart
sudo systemctl restart greengrass

# Check status
sudo systemctl status greengrass
```

### Model Deployment Fails with S3 Access Error

**Error:** `S3_HEAD_OBJECT_ACCESS_DENIED`

**Solution:** Verify the device role has the correct policy attached:
```bash
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole
```

Should include `DDAPortalComponentAccessPolicy`.

### Can't Connect to Device

```bash
# Check security group allows SSH (port 22)
aws ec2 describe-security-groups --group-ids sg-xxxxx

# Check device is running
aws ec2 describe-instances --instance-ids i-xxxxx

# Verify key permissions
chmod 400 device-key.pem
```

### Inference Endpoint Not Responding

```bash
# Check if LocalServer is running
ps aux | grep LocalServer

# Check LocalServer logs
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log

# Check if port 5000 is listening
netstat -tlnp | grep 5000
```

### Docker Permission Errors

**Error:** `permission denied while trying to connect to Docker daemon`

**Solution:** Add the Greengrass user to the docker group:
```bash
sudo usermod -aG docker ggc_user
sudo systemctl restart greengrass
```

### Component Deployment Fails

**Error:** Component fails to deploy or stays in `INSTALLING` state

**Troubleshooting steps:**
```bash
# Check component logs
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log

# Check if component artifact can be downloaded
curl -v https://s3.amazonaws.com/dda-component-region-account/component-name/version/artifact.zip

# Verify S3 bucket policy allows access
aws s3api get-bucket-policy --bucket dda-component-region-account

# Check device role has S3 permissions
aws iam get-role-policy --role-name GreengrassV2TokenExchangeRole --policy-name GreengrassComponentS3Access
```

### S3 Access Denied

**Error:** `AccessDeniedException` when downloading models or components

**Solution:** Verify the device role has proper S3 permissions:
```bash
# Check attached policies
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole

# Check inline policies
aws iam list-role-policies --role-name GreengrassV2TokenExchangeRole

# View specific policy
aws iam get-role-policy --role-name GreengrassV2TokenExchangeRole --policy-name GreengrassComponentS3Access
```

### Frontend Not Accessible

**Error:** Cannot reach http://device-ip:3000

**Troubleshooting:**
```bash
# Check if frontend container is running
docker ps | grep frontend

# Check container logs
docker logs $(docker ps -q -f "ancestor=dda-frontend")

# Verify port 3000 is listening
sudo netstat -tlnp | grep 3000

# Check security group allows port 3000
aws ec2 describe-security-groups --group-ids sg-xxxxx | grep 3000

# Try SSH tunnel instead
ssh -i "device-key.pem" -L 3000:localhost:3000 ubuntu@device-ip
# Then open http://localhost:3000
```

### Model Loading Issues

**Error:** Model fails to load in Triton server

**Troubleshooting:**
```bash
# Check Triton server logs
docker logs $(docker ps -q -f "ancestor=triton")

# Verify model files exist
ls -la /aws_dda/greengrass/v2/packages/artifacts/model-*/

# Check model format compatibility
file /aws_dda/greengrass/v2/packages/artifacts/model-*/model.tar.gz

# Verify model was compiled for correct architecture
# For ARM64: should be compiled with ARM64 target
# For x86_64: should be compiled with x86_64 target
```

### Database Errors

**Error:** SQLite database locked or corrupted

**Solution:**
```bash
# Check database status
sqlite3 /aws_dda/greengrass/v2/config/dda.db ".tables"

# Backup and reset database
cp /aws_dda/greengrass/v2/config/dda.db /aws_dda/greengrass/v2/config/dda.db.backup
rm /aws_dda/greengrass/v2/config/dda.db

# Restart Greengrass to recreate database
sudo systemctl restart greengrass
```

### GStreamer Pipeline Issues

**Error:** GStreamer pipeline crashes with segmentation fault or pipeline errors

**Common GStreamer Issues:**

1. **Pipeline crashes (SIGSEGV):**
```bash
# Enable GStreamer debugging
export GST_DEBUG=3
gst-launch-1.0 -v videotestsrc ! autovideosink

# Check for missing plugins
gst-inspect-1.0 | grep -i plugin-name

# Install missing plugins
sudo apt-get install gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

2. **Camera not detected:**
```bash
# List available video devices
v4l2-ctl --list-devices

# Test camera directly
gst-launch-1.0 v4l2src device=/dev/video0 ! autovideosink

# Check camera permissions
ls -la /dev/video*
sudo usermod -aG video ggc_user
```

3. **Pipeline doesn't want to pause:**
```bash
# Check for incompatible caps
gst-launch-1.0 -v videotestsrc ! video/x-raw,format=YUY2,width=640,height=480 ! autovideosink

# Verify plugin versions
gst-inspect-1.0 --version
```

4. **Low video quality in Docker:**
```bash
# Ensure proper device access in Docker
docker run --device /dev/video0 --device /dev/dri ...

# Check for hardware acceleration
docker run --gpus all ...  # For GPU devices
```

### Logs and Monitoring

**View system logs:**
```bash
# Greengrass Nucleus
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log

# LocalServer component
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log

# Model component
tail -f /aws_dda/greengrass/v2/logs/model-*.log

# All logs
ls -lah /aws_dda/greengrass/v2/logs/

# Search for errors
grep -i "error\|failed\|exception" /aws_dda/greengrass/v2/logs/*.log
```

**Monitor device health:**
```bash
# Check disk space
df -h /aws_dda/

# Check memory usage
free -h

# Check CPU usage
top -b -n 1 | head -20

# Check network connectivity
ping 8.8.8.8
curl -I https://s3.amazonaws.com

# Check AWS connectivity
aws sts get-caller-identity
```

**CloudWatch Logs:**
```bash
# View device logs in CloudWatch
aws logs tail /aws/greengrass/GreengrassSystemComponent/us-east-1/device-name --follow

# Search for specific errors
aws logs filter-log-events \
  --log-group-name /aws/greengrass/GreengrassSystemComponent/us-east-1/device-name \
  --filter-pattern "ERROR"
```

## Advanced Configuration

### Custom Instance Type

For larger models or higher throughput, use a larger instance:

```bash
./launch-edge-device.sh us-east-1 my-device t3.large
```

Recommended instance types:
- `t3.medium` - Small models, testing (default)
- `t3.large` - Medium models, production
- `g4dn.xlarge` - GPU acceleration (NVIDIA)

### Multiple Devices

To provision multiple devices:

```bash
for i in {1..3}; do
  ./launch-edge-device.sh us-east-1 device-$i
done
```

### Custom Greengrass Configuration

Edit `setup_station.sh` to customize:
- Greengrass version (line ~200)
- Python version (line ~100)
- System users and groups (line ~300)
- Directory permissions (line ~400)

## Security Considerations

- Keep SSH keys secure (chmod 400)
- Use security groups to restrict access
- Enable CloudWatch Logs for audit trails
- Regularly update device software
- Use IAM roles instead of access keys
- Enable encryption for S3 buckets

## Support

For issues or questions:
1. Check device logs: `/aws_dda/greengrass/v2/logs/`
2. Run diagnostics: `/aws_dda/check-cloudwatch-logging.sh`
3. Review CloudWatch Logs in AWS console
4. Check DDA Portal documentation: `edge-cv-portal/SINGLE_ACCOUNT_SETUP_GUIDE.md`

## Related Documentation

- [DDA Portal Setup Guide](../edge-cv-portal/SINGLE_ACCOUNT_SETUP_GUIDE.md)
- [AWS Greengrass Documentation](https://docs.aws.amazon.com/greengrass/)
- [AWS IoT Core Documentation](https://docs.aws.amazon.com/iot-core/)
