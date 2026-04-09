#!/bin/bash
set -e

ACCOUNT_ID="632852507243"
REGION="ap-northeast-2"
REPO_NAME="shiftee-exit"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:latest"

echo "=== ECR 로그인 ==="
aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

echo "=== Docker 이미지 빌드 ==="
docker build -t ${REPO_NAME} .

echo "=== 태그 지정 ==="
docker tag ${REPO_NAME}:latest ${IMAGE_URI}

echo "=== ECR에 Push ==="
docker push ${IMAGE_URI}

echo "=== 완료! ==="
echo "Image: ${IMAGE_URI}"
