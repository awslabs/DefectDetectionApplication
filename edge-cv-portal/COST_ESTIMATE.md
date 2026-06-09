# DDA Portal Cost Estimate

## Architecture Overview

The DDA Portal uses a multi-account architecture:
- **Portal Account**: Hosts the web portal, backend APIs, and shared components
- **UseCase Account(s)**: Customer accounts for ML training, edge deployments, and data storage

---

## Portal Account Costs (Monthly)

### Core Infrastructure

| Service | Component | Usage | Monthly Cost |
|---------|-----------|-------|--------------|
| **CloudFront** | Frontend CDN | 1TB data transfer, 10M requests | $85 |
| **S3** | Frontend hosting | 5GB storage, 100K requests | $0.50 |
| **S3** | Component artifacts | 10GB storage, 1M requests | $0.50 |
| **Cognito** | User authentication | 100 active users | $27.50 |
| **API Gateway** | REST API | 1M requests | $3.50 |
| **Lambda** | Backend functions (10 functions) | 10M requests, 512MB, 1s avg | $20 |
| **DynamoDB** | Portal database | 5GB storage, 1M reads, 500K writes | $3.75 |
| **EventBridge** | Cross-account events | 1M events | $1 |
| **CloudWatch Logs** | Application logs | 10GB ingestion, 5GB storage | $5.50 |
| **Route53** | DNS hosting | 1 hosted zone, 1M queries | $1.50 |
| **ACM** | SSL certificates | 1 certificate | $0 (free) |

**Portal Account Subtotal: ~$148/month**

---

## UseCase Account Costs (Per Account, Monthly)

### ML Training & Compilation

| Service | Component | Usage | Monthly Cost |
|---------|-----------|-------|--------------|
| **SageMaker Training** | Model training | 10 jobs/month, ml.p3.2xlarge, 2h each | $61 |
| **SageMaker Compilation** | Edge optimization | 10 jobs/month, 30min each | $5 |
| **S3** | Training data & models | 100GB storage, 10M requests | $3 |
| **Ground Truth** | Data labeling | 1,000 images/month, private workforce | $60 |

**ML Training Subtotal: ~$129/month** (variable based on training frequency)

### Edge Device Management (Per Device)

| Service | Component | Usage | Monthly Cost |
|---------|-----------|-------|--------------|
| **IoT Core** | Device connectivity | 1 device, 1M messages | $0.80 |
| **IoT Greengrass** | Edge runtime | 1 device | $0.16/device |
| **S3** | Inference results | 10GB/month, 100K uploads | $0.30 |
| **CloudWatch Logs** | Device logs | 5GB ingestion, 2GB storage | $2.75 |

**Per Device Subtotal: ~$4/month**

### Infrastructure (Fixed)

| Service | Component | Usage | Monthly Cost |
|---------|-----------|-------|--------------|
| **IAM** | Roles & policies | 5 roles | $0 (free) |
| **S3** | Inference results bucket | Lifecycle policies | $0 (included) |
| **EventBridge** | SageMaker events | 10K events | $0.01 |

**UseCase Account Base: ~$129/month (training) + $4/device**

---

## Cost Scenarios

### Scenario 1: Small Deployment
- **Portal Account**: 1 portal
- **UseCase Accounts**: 2 accounts
- **Devices**: 5 devices per account (10 total)
- **Training**: 5 jobs/month per account

| Component | Cost |
|-----------|------|
| Portal Account | $148 |
| UseCase Account 1 (base + 5 devices) | $129 + $20 = $149 |
| UseCase Account 2 (base + 5 devices) | $129 + $20 = $149 |
| **Total** | **$446/month** |

### Scenario 2: Medium Deployment
- **Portal Account**: 1 portal
- **UseCase Accounts**: 5 accounts
- **Devices**: 20 devices per account (100 total)
- **Training**: 10 jobs/month per account

| Component | Cost |
|-----------|------|
| Portal Account | $148 |
| UseCase Accounts (5 × [$129 + $80]) | $1,045 |
| **Total** | **$1,193/month** |

### Scenario 3: Large Deployment
- **Portal Account**: 1 portal
- **UseCase Accounts**: 20 accounts
- **Devices**: 50 devices per account (1,000 total)
- **Training**: 20 jobs/month per account

| Component | Cost |
|-----------|------|
| Portal Account | $148 |
| UseCase Accounts (20 × [$258 + $200]) | $9,160 |
| **Total** | **$9,308/month** |

---

## Cost Optimization Strategies

### 1. S3 Lifecycle Policies
- **Current**: Inference results kept for 90 days
- **Optimization**: Move to Glacier after 30 days
- **Savings**: ~40% on S3 storage costs

### 2. CloudWatch Logs Retention
- **Current**: Indefinite retention
- **Optimization**: 30-day retention for device logs
- **Savings**: ~60% on CloudWatch Logs costs

### 3. Reserved Capacity
- **SageMaker Savings Plans**: 1-year commitment
- **Savings**: Up to 64% on training costs
- **Example**: $61/month → $22/month per training workload

### 4. Spot Instances for Training
- **Current**: On-demand ml.p3.2xlarge
- **Optimization**: Use Spot instances
- **Savings**: Up to 70% on training costs
- **Example**: $61/month → $18/month

### 5. S3 Intelligent-Tiering
- **Optimization**: Auto-move infrequently accessed data
- **Savings**: 40-68% on storage costs
- **Best for**: Training data, old inference results

### 6. Lambda Reserved Concurrency
- **Current**: On-demand Lambda
- **Optimization**: Provisioned concurrency for high-traffic APIs
- **Trade-off**: Higher base cost, lower per-request cost

---

## Cost Breakdown by Category

### Portal Account (Monthly)
```
Compute (Lambda):           $20  (13%)
Storage (S3):               $1   (1%)
Networking (CloudFront):    $85  (57%)
Database (DynamoDB):        $4   (3%)
Auth (Cognito):             $28  (19%)
API (API Gateway):          $4   (3%)
Monitoring (CloudWatch):    $6   (4%)
```

### UseCase Account (Monthly, 10 devices)
```
ML Training:                $129 (76%)
Edge Devices:               $40  (24%)
  - IoT Core:               $8   (5%)
  - Greengrass:             $2   (1%)
  - S3 (inference):         $3   (2%)
  - CloudWatch Logs:        $27  (16%)
```

---

## Variable Cost Factors

### High Impact
1. **Number of devices**: $4/device/month
2. **Training frequency**: $6/job (training) + $0.50/job (compilation)
3. **Data transfer**: CloudFront egress costs
4. **Inference volume**: S3 storage and requests

### Medium Impact
1. **Number of users**: Cognito costs scale with MAU
2. **API request volume**: API Gateway + Lambda costs
3. **Log retention**: CloudWatch Logs storage
4. **Training instance type**: ml.p3.2xlarge vs ml.p3.8xlarge

### Low Impact
1. **Number of usecase accounts**: Minimal incremental cost
2. **Component versions**: S3 storage for artifacts
3. **Deployment frequency**: IoT Core messages

---

## Cost Monitoring & Alerts

### Recommended CloudWatch Alarms

1. **Portal Account**:
   - Lambda invocations > 15M/month
   - CloudFront data transfer > 1.5TB/month
   - DynamoDB consumed capacity > 80%

2. **UseCase Account**:
   - SageMaker training hours > 25h/month
   - S3 storage > 150GB
   - IoT Core messages > 1.5M/device/month

### Cost Allocation Tags

Use these tags for cost tracking:
- `dda-portal:managed=true`
- `dda-portal:usecase-id={id}`
- `dda-portal:component-type={type}`
- `dda-portal:environment={prod|dev}`

---

## Free Tier Benefits (First 12 Months)

| Service | Free Tier | Monthly Value |
|---------|-----------|---------------|
| Lambda | 1M requests, 400K GB-seconds | $20 |
| S3 | 5GB storage, 20K GET, 2K PUT | $0.50 |
| CloudWatch Logs | 5GB ingestion | $2.50 |
| DynamoDB | 25GB storage, 25 RCU, 25 WCU | $3 |
| **Total Savings** | | **$26/month** |

---

## Enterprise Pricing Considerations

### AWS Enterprise Support
- **Cost**: 10% of monthly AWS spend (minimum $15K/month)
- **Benefits**: TAM, 15-min response time, architectural guidance
- **Recommended for**: >$150K/year AWS spend

### AWS Savings Plans
- **Compute Savings Plan**: 1 or 3-year commitment
- **Discount**: Up to 17% (1-year) or 66% (3-year)
- **Applies to**: Lambda, SageMaker

### Volume Discounts
- **S3**: Tiered pricing after 50TB/month
- **CloudFront**: Custom pricing for >10TB/month
- **Contact AWS**: For >$10K/month spend

---

## Cost Comparison: Self-Hosted vs DDA Portal

### Self-Hosted (EC2-based)
- **Infrastructure**: $500-1,000/month (EC2, RDS, ALB)
- **Maintenance**: 20-40 hours/month engineering time
- **Scaling**: Manual capacity planning
- **Total**: $1,500-3,000/month (including labor)

### DDA Portal (Serverless)
- **Infrastructure**: $148/month (portal) + $129/usecase
- **Maintenance**: Minimal (AWS-managed)
- **Scaling**: Automatic
- **Total**: $277/month (1 usecase)

**Savings**: 60-80% for small-medium deployments

---

## Summary

### Typical Monthly Costs

| Deployment Size | Portal | UseCases | Devices | Total |
|----------------|--------|----------|---------|-------|
| **Pilot** | $148 | 1 × $129 | 3 × $4 | **$289** |
| **Small** | $148 | 2 × $149 | 10 × $4 | **$446** |
| **Medium** | $148 | 5 × $209 | 100 × $4 | **$1,193** |
| **Large** | $148 | 20 × $458 | 1,000 × $4 | **$9,308** |

### Key Takeaways

1. **Portal account is fixed cost**: ~$148/month regardless of scale
2. **UseCase accounts scale with training**: $129/month base + training costs
3. **Devices are cheap**: Only $4/device/month
4. **Training is the biggest variable**: Optimize with Spot instances
5. **Free tier helps**: ~$26/month savings for first year

### Cost Per Device (Fully Loaded)

- **Small deployment** (10 devices): $45/device/month
- **Medium deployment** (100 devices): $12/device/month
- **Large deployment** (1,000 devices): $9/device/month

**Economies of scale**: Cost per device decreases significantly with fleet size.

---

## Additional Resources

- [AWS Pricing Calculator](https://calculator.aws/)
- [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/)
- [AWS Budgets](https://aws.amazon.com/aws-cost-management/aws-budgets/)
- [SageMaker Pricing](https://aws.amazon.com/sagemaker/pricing/)
- [IoT Core Pricing](https://aws.amazon.com/iot-core/pricing/)
