# AWS Setup for Quarry

Quarry works fully offline with local OCR and ONNX embedding. AWS is only needed for cloud-accelerated OCR (Textract) and cloud-accelerated embedding (SageMaker). This guide walks you through creating a dedicated IAM user with least-privilege permissions.

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
export AWS_DEFAULT_REGION="us-east-1"
```

## 3. Attach Permissions

Create a custom policy with only what quarry needs. Go to **IAM → Policies → Create policy**, switch to JSON, and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TextractOCR",
      "Effect": "Allow",
      "Action": [
        "textract:DetectDocumentText",
        "textract:StartDocumentTextDetection",
        "textract:GetDocumentTextDetection"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3ForTextract",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    },
    {
      "Sid": "SageMakerEmbedding",
      "Effect": "Allow",
      "Action": [
        "sagemaker:DescribeEndpoint",
        "sagemaker:InvokeEndpoint"
      ],
      "Resource": "arn:aws:sagemaker:us-east-1:YOUR-ACCOUNT-ID:endpoint/quarry-embedding"
    }
  ]
}
```

Replace `YOUR-BUCKET-NAME` with your S3 bucket and `YOUR-ACCOUNT-ID` with your 12-digit AWS account ID (visible in the top-right of the console).

Name the policy `quarry-app-policy`, then attach it to the `quarry-app` user via **Users → quarry-app → Permissions → Add permissions → Attach policies directly**.

## 4. Deploy the SageMaker Endpoint (One-Time)

The CloudFormation deploy creates IAM roles and SageMaker resources. Run this from your **root account** (or a user with `AdministratorAccess`) since the `quarry-app` user intentionally lacks these broad permissions:

```bash
aws cloudformation deploy \
  --template-file infra/sagemaker-embedding.yaml \
  --stack-name quarry-embedding \
  --capabilities CAPABILITY_NAMED_IAM
```

After this completes (~5-10 min), the `quarry-app` user can invoke the endpoint.

## 5. Verify

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
| Textract OCR | `OCR_BACKEND=textract`, `S3_BUCKET` | TextractOCR + S3ForTextract |
| SageMaker embedding | `EMBEDDING_BACKEND=sagemaker`, `SAGEMAKER_ENDPOINT_NAME` | SageMakerEmbedding |

You can enable features incrementally. Start with local-only, add Textract when you need better OCR quality, add SageMaker when batch ingestion speed matters.

## Security Notes

- **Lock down your root account**: Enable MFA on root (IAM → Security credentials → MFA). Use root only for billing and account-level changes.
- **Rotate keys periodically**: IAM → Users → quarry-app → Security credentials → rotate access keys every 90 days.
- **Never commit credentials**: Quarry reads from environment variables only. Keep them in your shell profile or a secrets manager, never in `.env` files checked into git.
- **Scope the S3 bucket**: The policy above limits S3 access to one bucket. Create a dedicated bucket (e.g. `quarry-textract-uploads`) rather than granting access to existing buckets.
- **Scope the SageMaker resource**: The policy ARN is scoped to the specific endpoint name. If you change the endpoint name in the CloudFormation parameters, update the policy to match.
