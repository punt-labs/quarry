# AWS Setup for Quarry

Quarry works fully offline with local OCR and ONNX embedding. AWS is only needed for cloud-accelerated OCR (Textract) and cloud-accelerated embedding (SageMaker). This guide walks you through creating a dedicated IAM user with least-privilege permissions.

## Region Strategy

All AWS resources must be in the **same region**. Quarry uses `AWS_DEFAULT_REGION` for both Textract and SageMaker at runtime. The deploy script (`manage-stack.sh`) inherits this same variable by default.

Pick one region and use it everywhere:

```bash
export AWS_DEFAULT_REGION="us-west-2"   # or whichever region you prefer
```

| Resource | Controlled By | Must Match |
|----------|--------------|------------|
| Textract API calls | `AWS_DEFAULT_REGION` | S3 bucket region |
| S3 bucket for Textract | Created in your chosen region | `AWS_DEFAULT_REGION` |
| SageMaker endpoint | `QUARRY_DEPLOY_REGION` (defaults to `AWS_DEFAULT_REGION`) | `AWS_DEFAULT_REGION` |
| SageMaker model artifacts bucket | Auto-created by `manage-stack.sh` in deploy region | SageMaker endpoint region |

If you see `AccessDeniedException` or `endpoint not found` errors, check that all resources are in the same region.

## 1. Create an IAM User

Sign in to the AWS Console as root, then:

1. Go to **IAM → Users → Create user**
2. Name it `quarry-app` (or similar)
3. Do **not** enable console access — this user is CLI-only
4. Click **Next**, skip adding to groups for now, click **Create user**

## 2. Create Access Keys

1. Open the `quarry-app` user → **Security credentials** tab
2. **Create access key** → select **Command Line Interface (CLI)**
3. Save the Access Key ID and Secret Access Key — you won't see the secret again

Set them in your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-west-2"
```

## 3. Attach Permissions

Create a custom policy with only what quarry needs. Go to **IAM → Policies → Create policy**, switch to JSON, and paste the contents of [`quarry-iam-policy.json`](quarry-iam-policy.json).

Replace `YOUR-BUCKET-NAME` with your S3 bucket and `YOUR-ACCOUNT-ID` with your 12-digit AWS account ID (visible in the top-right of the console).

Name the policy `quarry-app-policy`, then attach it to the `quarry-app` user via **Users → quarry-app → Permissions → Add permissions → Attach policies directly**.

## 4. Create an S3 Bucket for Textract

Textract's async API requires an S3 bucket in the same region:

```bash
aws s3api create-bucket \
  --bucket YOUR-BUCKET-NAME \
  --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2
```

Set `S3_BUCKET` in your environment:

```bash
export S3_BUCKET="YOUR-BUCKET-NAME"
```

## 5. Deploy the SageMaker Endpoint (One-Time)

The management script creates IAM roles and SageMaker resources. Run this from your **root account** (or a user with `AdministratorAccess`) since the `quarry-app` user intentionally lacks these broad permissions:

```bash
./infra/manage-stack.sh deploy              # serverless (default, pay-per-request)
./infra/manage-stack.sh deploy realtime     # persistent instance (~$0.12/hr)
```

The script:
- Inherits `AWS_DEFAULT_REGION` from your environment (so it deploys to the same region as Textract)
- Auto-creates an S3 bucket for model artifacts in that region
- Packages the custom inference handler and deploys the CloudFormation stack

After this completes (~5-10 min), the `quarry-app` user can invoke the endpoint.

Tear down when not in use:

```bash
./infra/manage-stack.sh destroy
```

## 6. Verify

```bash
# Source your credentials, then:
quarry doctor
```

Expected output includes:

```
  ✓ AWS credentials: AKIA**** (via explicit-keys)
  ✓ SageMaker endpoint: quarry-embedding (InService)
```

## What Each Feature Needs

| Feature | Env Vars | Permissions |
|---------|----------|-------------|
| Local OCR + ONNX embedding | None | None |
| Textract OCR | `OCR_BACKEND=textract`, `S3_BUCKET`, `AWS_DEFAULT_REGION` | TextractOCR + S3ForTextract |
| SageMaker embedding | `EMBEDDING_BACKEND=sagemaker`, `SAGEMAKER_ENDPOINT_NAME`, `AWS_DEFAULT_REGION` | SageMakerEmbedding |

You can enable features incrementally. Start with local-only, add Textract when you need better OCR quality, add SageMaker when batch ingestion speed matters.

## Security Notes

- **Lock down your root account**: Enable MFA on root (IAM → Security credentials → MFA). Use root only for billing and account-level changes.
- **Rotate keys periodically**: IAM → Users → quarry-app → Security credentials → rotate access keys every 90 days.
- **Never commit credentials**: Quarry reads from environment variables only. Keep them in your shell profile or a secrets manager, never in `.env` files checked into git.
- **Scope the S3 bucket**: The policy limits S3 access to one bucket. Create a dedicated bucket rather than granting access to existing buckets.
- **Scope the SageMaker resource**: The policy ARN is scoped to the specific endpoint name. If you change the endpoint name in the CloudFormation parameters, update the policy to match.
