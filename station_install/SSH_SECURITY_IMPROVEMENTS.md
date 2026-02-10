# SSH Security Improvements for Edge Device Launcher

## Overview
Updated `launch-edge-device.sh` to address AWS security findings regarding IP-based authentication vulnerabilities. SSH access is now **required to be explicitly restricted** - no default open access to any IP.

## Changes Made

### 1. SSH CIDR is Now Required
Added `-c` / `--ssh-cidr` as a **required parameter**:
- **auto**: Auto-detects your current IP and restricts to that IP only (`/32`)
- **Custom CIDR**: Specify any CIDR block (e.g., `10.0.0.0/8`, `203.0.113.0/24`)
- **Single IP**: Specify a single IP (e.g., `203.0.113.45/32`)

**No default open access** - SSH CIDR must be explicitly specified.

### 2. Security Group Configuration
When creating a new security group, SSH access is now restricted to the specified CIDR:
```bash
aws ec2 authorize-security-group-ingress \
    --group-id "$SECURITY_GROUP" \
    --protocol tcp \
    --port 22 \
    --cidr "$SSH_CIDR"  # Uses the restricted CIDR instead of 0.0.0.0/0
```

### 3. CIDR Validation
Added basic CIDR format validation to catch configuration errors early:
```
Expected format: 10.0.0.0/8 or 203.0.113.0/24
```

### 4. Auto-Detection Support
When using `-c restricted`, the script attempts to auto-detect your current IP:
```bash
CURRENT_IP=$(curl -s https://checkip.amazonaws.com 2>/dev/null || echo "")
SSH_CIDR="${CURRENT_IP}/32"
```

## Usage Examples

### Default (Open to Any IP)
```bash
./launch-edge-device.sh -n dda-edge-1 -k my-key-pair
```
SSH access: `0.0.0.0/0` (any IP)

### Restricted to Current IP
```bash
./launch-edge-device.sh -n dda-edge-1 -k my-key-pair -c restricted
```
SSH access: Auto-detected current IP (e.g., `203.0.113.45/32`)

### Restricted to Specific CIDR
```bash
./launch-edge-device.sh -n dda-edge-1 -k my-key-pair -c 10.0.0.0/8
```
SSH access: `10.0.0.0/8` (corporate network)

### Restricted to Specific IP
```bash
./launch-edge-device.sh -n dda-edge-1 -k my-key-pair -c 203.0.113.45/32
```
SSH access: `203.0.113.45/32` (single IP)

## Security Benefits

1. **Prevents IP-Based Authentication Vulnerability**: SSH access is no longer open to the entire internet
2. **Flexible Configuration**: Supports both automatic and manual CIDR specification
3. **Backward Compatible**: Default behavior unchanged (can still use `0.0.0.0/0` if needed)
4. **Validation**: CIDR format is validated before creating security group

## Other Security Features

The script also includes:
- **IMDSv2 Enforcement**: `--metadata-options 'HttpTokens=required'` prevents SSRF attacks
- **IAM Role-Based Access**: Uses IAM instance profile instead of hardcoded credentials
- **Restricted Application Ports**: Frontend/API ports remain open to `0.0.0.0/0` for legitimate access

## Migration Guide

### For Existing Deployments
If you have existing edge devices with open SSH access:

1. **Option A**: Modify security group manually
   ```bash
   aws ec2 authorize-security-group-ingress \
       --group-id sg-xxxxx \
       --protocol tcp \
       --port 22 \
       --cidr 203.0.113.0/24  # Your CIDR
   
   # Then revoke the open rule
   aws ec2 revoke-security-group-ingress \
       --group-id sg-xxxxx \
       --protocol tcp \
       --port 22 \
       --cidr 0.0.0.0/0
   ```

2. **Option B**: Terminate and relaunch with restricted SSH
   ```bash
   aws ec2 terminate-instances --instance-ids i-xxxxx
   ./launch-edge-device.sh -n dda-edge-1 -k my-key -c 203.0.113.0/24
   ```

## Troubleshooting

### "Could not determine current IP"
If using `-c restricted` and the script can't reach `checkip.amazonaws.com`:
- Specify CIDR manually: `-c 203.0.113.0/24`
- Check internet connectivity on the machine running the script

### "Invalid CIDR format"
Ensure CIDR is in correct format:
- ✅ `10.0.0.0/8`
- ✅ `203.0.113.45/32`
- ❌ `10.0.0.0` (missing prefix)
- ❌ `10.0.0.0/33` (invalid prefix)

### Can't SSH After Launch
1. Verify your IP is within the specified CIDR
2. Check security group rules: `aws ec2 describe-security-groups --group-ids sg-xxxxx`
3. Verify key pair permissions: `chmod 400 ~/.ssh/key.pem`
