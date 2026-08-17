# GPU Quota Audit — Portal_Account 164152369890, us-east-1

Captured 2026-08-17T04:33:54Z by `benchmark-harness/quota_audit.py` (read-only, Req 3.4).

| Service | Quota | Code | Current value |
|---|---|---|---|
| ec2 | Running On-Demand G and VT instances (vCPUs) | L-DB2E81BA | 768.0 |
| ec2 | Running On-Demand P instances (vCPUs) | L-417A185B | 768.0 |
| sagemaker | ml.g5.xlarge for endpoint usage | L-1928E07B | 4.0 |
| sagemaker | ml.g6.xlarge for endpoint usage | L-D470D954 | 1.0 |
| sagemaker | ml.g6e.xlarge for endpoint usage | L-B0729CB4 | 0.0 |
| sagemaker | ml.g6e.2xlarge for endpoint usage | L-F8D7F460 | 0.0 |
| sagemaker | ml.g6e.4xlarge for endpoint usage | L-93531071 | 0.0 |
| sagemaker | ml.p4d.24xlarge for endpoint usage | L-09F79647 | 0.0 |

Interpretation for the hosting comparison (task 7.2): the EC2 G/VT
vCPU quota bounds concurrent g5/g6/g6e benchmark and future always-on
instances (g6e.xlarge = 4 vCPUs, g6e.2xlarge = 8, g6e.4xlarge = 16);
the P quota bounds any p4d fallback (96 vCPUs per p4d.24xlarge).
SageMaker per-instance-type endpoint quotas bound future endpoint
hosting; zero/absent values need quota increase requests before a
production implementation.

---

> **⚠️ SUPERSEDED for the SageMaker g6e rows (task 7.1, 2026-08-17T23:32:17Z).**
> The `ml.g6e.* for endpoint usage` values of `0.0` recorded above are **wrong**.
> A live re-audit (`aws service-quotas list-service-quotas --service-code
> sagemaker --region us-east-1`) shows `ml.g6e.xlarge` = 4, `ml.g6e.2xlarge` = 4
> (both raised from 1 by the increases approved 2026-08-17T22:59:29Z),
> `ml.g6e.4xlarge` / `8xlarge` / `12xlarge` / `16xlarge` = 1, and only
> `ml.g6e.24xlarge` / `48xlarge` / p4d / p4de / p5 = 0. The authoritative record
> is `../quota-audit.md`; the EC2 vCPU rows above are unchanged and still valid.
> Consequence: the "quota is 0" reason recorded elsewhere for skipping optional
> task 4.4 was incorrect — see `../quota-audit.md` §2.
