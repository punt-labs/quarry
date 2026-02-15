#!/usr/bin/env bash
# Manage the quarry-embedding SageMaker CloudFormation stack.
#
# Usage:
#   ./infra/manage-stack.sh deploy [serverless|realtime] [Key=Value ...]
#   ./infra/manage-stack.sh destroy
#   ./infra/manage-stack.sh status

set -euo pipefail

STACK_NAME="quarry-embedding"
PROFILE="${QUARRY_DEPLOY_PROFILE:-admin}"
INFRA_DIR="$(cd "$(dirname "$0")" && pwd)"
S3_KEY="sagemaker/quarry-embedding/model.tar.gz"

# Region: use QUARRY_DEPLOY_REGION, fall back to AWS_DEFAULT_REGION, then us-east-1.
# This ensures the SageMaker endpoint deploys to the same region as your Textract
# S3 bucket and app runtime, avoiding cross-region mismatches.
REGION="${QUARRY_DEPLOY_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

ensure_bucket() {
  # S3 bucket for model artifacts (must be in the same region as the endpoint).
  # Set QUARRY_MODEL_BUCKET or the script auto-creates a region-specific one.
  if [ -n "${QUARRY_MODEL_BUCKET:-}" ]; then
    S3_BUCKET="$QUARRY_MODEL_BUCKET"
  else
    ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
    # Include region in bucket name so each region gets its own artifact bucket.
    S3_BUCKET="quarry-models-${REGION}-${ACCOUNT_ID}"
    if ! aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" --profile "$PROFILE" 2>/dev/null; then
      echo "Creating S3 bucket $S3_BUCKET in $REGION..."
      if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION" --profile "$PROFILE"
      else
        aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION" --profile "$PROFILE" \
          --create-bucket-configuration LocationConstraint="$REGION"
      fi
    fi
  fi
}

upload_inference_code() {
  echo "Packaging custom inference handler..."
  TMPTAR="$(mktemp /tmp/quarry-model-XXXXXX).tar.gz"
  trap 'rm -f "$TMPTAR"' EXIT
  tar -czf "$TMPTAR" -C "$INFRA_DIR/sagemaker-inference" code/
  echo "Uploading to s3://$S3_BUCKET/$S3_KEY..."
  aws s3 cp "$TMPTAR" "s3://$S3_BUCKET/$S3_KEY" \
    --region "$REGION" \
    --profile "$PROFILE"
  rm -f "$TMPTAR"
  trap - EXIT
}

cleanup_rollback() {
  local status
  status=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")
  if [ "$status" = "ROLLBACK_COMPLETE" ]; then
    echo "Stack in ROLLBACK_COMPLETE — deleting first..."
    aws cloudformation delete-stack \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --profile "$PROFILE"
    aws cloudformation wait stack-delete-complete \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --profile "$PROFILE"
  fi
}

case "${1:-}" in
  deploy)
    MODE="${2:-serverless}"
    case "$MODE" in
      serverless) TEMPLATE="$INFRA_DIR/sagemaker-serverless.yaml" ;;
      realtime)   TEMPLATE="$INFRA_DIR/sagemaker-realtime.yaml" ;;
      *)
        echo "Unknown mode: $MODE (expected 'serverless' or 'realtime')"
        exit 1
        ;;
    esac
    # Extra args start after the mode argument (Key=Value pairs)
    shift 2 2>/dev/null || shift 1

    ensure_bucket
    upload_inference_code
    echo "Deploying $STACK_NAME ($MODE) in $REGION..."
    cleanup_rollback
    aws cloudformation deploy \
      --template-file "$TEMPLATE" \
      --stack-name "$STACK_NAME" \
      --capabilities CAPABILITY_NAMED_IAM \
      --region "$REGION" \
      --profile "$PROFILE" \
      --parameter-overrides "ModelDataBucket=$S3_BUCKET" "ModelDataKey=$S3_KEY" \
      "$@"
    echo "Done. Run 'quarry doctor' to verify."
    ;;
  destroy)
    echo "Deleting $STACK_NAME in $REGION..."
    aws cloudformation delete-stack \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --profile "$PROFILE"
    aws cloudformation wait stack-delete-complete \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --profile "$PROFILE"
    echo "Stack deleted."
    ;;
  status)
    aws cloudformation describe-stacks \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query "Stacks[0].{Status:StackStatus,Created:CreationTime,Updated:LastUpdatedTime}" \
      --output table 2>/dev/null || echo "Stack does not exist."
    ;;
  *)
    echo "Usage: $0 <command> [mode] [extra-args...]"
    echo ""
    echo "Commands:"
    echo "  deploy [serverless|realtime]  Deploy the SageMaker endpoint (default: serverless)"
    echo "  destroy                       Delete the stack and all resources"
    echo "  status                        Show current stack status"
    echo ""
    echo "Environment variables:"
    echo "  QUARRY_DEPLOY_REGION   Deploy region (default: \$AWS_DEFAULT_REGION or us-east-1)"
    echo "  QUARRY_DEPLOY_PROFILE  AWS CLI profile (default: admin)"
    echo "  QUARRY_MODEL_BUCKET    S3 bucket for model artifacts (auto-created if unset)"
    echo ""
    echo "Extra Key=Value pairs after mode are appended to --parameter-overrides."
    echo ""
    echo "Examples:"
    echo "  $0 deploy                     # serverless, 3072 MB, pay-per-request"
    echo "  $0 deploy serverless          # same as above"
    echo "  $0 deploy realtime            # ml.m5.large, ~\$0.12/hr"
    echo "  $0 deploy realtime InstanceType=ml.t2.large"
    echo "  $0 destroy                    # tear down (do this when not in use)"
    exit 1
    ;;
esac
