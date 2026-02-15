#!/usr/bin/env bash
# Manage the quarry-embedding SageMaker CloudFormation stack.
#
# Usage:
#   ./infra/manage-stack.sh deploy [serverless|realtime] [-- extra-cfn-args...]
#   ./infra/manage-stack.sh destroy
#   ./infra/manage-stack.sh status

set -euo pipefail

STACK_NAME="quarry-embedding"
REGION="us-west-1"
PROFILE="admin"
INFRA_DIR="$(cd "$(dirname "$0")" && pwd)"
S3_BUCKET="quarry-models-975377310343"
S3_KEY="sagemaker/quarry-embedding/model.tar.gz"

upload_inference_code() {
  echo "Packaging custom inference handler..."
  local tmptar
  tmptar="$(mktemp /tmp/quarry-model-XXXXXX.tar.gz)"
  tar -czf "$tmptar" -C "$INFRA_DIR/sagemaker-inference" code/
  echo "Uploading to s3://$S3_BUCKET/$S3_KEY..."
  aws s3 cp "$tmptar" "s3://$S3_BUCKET/$S3_KEY" \
    --region "$REGION" \
    --profile "$PROFILE"
  rm -f "$tmptar"
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
    # Extra args start after the mode argument
    shift 2 2>/dev/null || shift 1

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
    echo "Extra args after mode are passed to 'cloudformation deploy'."
    echo ""
    echo "Examples:"
    echo "  $0 deploy                     # serverless, 3072 MB, pay-per-request"
    echo "  $0 deploy serverless          # same as above"
    echo "  $0 deploy realtime            # ml.m5.large, ~\$0.12/hr"
    echo "  $0 deploy realtime --parameter-overrides InstanceType=ml.t2.large"
    echo "  $0 destroy                    # tear down (do this when not in use)"
    exit 1
    ;;
esac
