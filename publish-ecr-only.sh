#!/bin/bash
set -e
set -o pipefail
# Publish-only (ECR + S3) for an already-built LocalServer component, WITHOUT
# rebuilding. Mirrors the >2GB ECR path in gdk-component-build-and-publish.sh,
# reusing the locally-built flask-app:latest / react-webapp:latest images and the
# existing custom-build staging dir. For aarch64 JetPack 5.

ARCH="aarch64"
COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64JP5"

echo "Checking AWS credentials..."
aws sts get-caller-identity >/dev/null || { echo "ERROR: AWS credentials invalid/expired"; exit 1; }

PUB_REGION=$(aws configure get region 2>/dev/null || true)
[ -z "$PUB_REGION" ] && PUB_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
PUB_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -z "$PUB_REGION" ] || [ -z "$PUB_ACCOUNT_ID" ] || [ "$PUB_ACCOUNT_ID" = "None" ]; then
    echo "ERROR: cannot resolve region/account (region='$PUB_REGION' account='$PUB_ACCOUNT_ID')"; exit 1
fi

ECR_REGISTRY="${PUB_ACCOUNT_ID}.dkr.ecr.${PUB_REGION}.amazonaws.com"
ECR_REPO_BACKEND="${ECR_REGISTRY}/dda/flask-app"
ECR_REPO_FRONTEND="${ECR_REGISTRY}/dda/react-webapp"
S3_BUCKET="dda-component-${PUB_REGION}-${PUB_ACCOUNT_ID}"

# Pre-flight: the images and staging dir must already exist (built, not rebuilt here).
docker image inspect flask-app:latest >/dev/null 2>&1 || { echo "ERROR: flask-app:latest not found"; exit 1; }
docker image inspect react-webapp:latest >/dev/null 2>&1 || { echo "ERROR: react-webapp:latest not found"; exit 1; }
STAGE_DIR="custom-build/${COMPONENT_NAME}"
[ -d "$STAGE_DIR" ] || { echo "ERROR: staging dir $STAGE_DIR not found"; exit 1; }

# Next version = bump patch of the latest registered version (start at 1.0.0).
LATEST_VERSION=$(aws greengrassv2 list-component-versions \
    --arn "arn:aws:greengrass:${PUB_REGION}:${PUB_ACCOUNT_ID}:components:${COMPONENT_NAME}" \
    --query 'componentVersions[0].componentVersion' --output text 2>/dev/null || echo "None")
if [ "$LATEST_VERSION" = "None" ] || [ -z "$LATEST_VERSION" ]; then
    COMPONENT_VERSION="1.0.0"
else
    V_MAJOR=$(echo "$LATEST_VERSION" | cut -d. -f1)
    V_MINOR=$(echo "$LATEST_VERSION" | cut -d. -f2)
    V_PATCH=$(echo "$LATEST_VERSION" | cut -d. -f3)
    COMPONENT_VERSION="${V_MAJOR}.${V_MINOR}.$((V_PATCH + 1))"
fi
echo "Latest registered: ${LATEST_VERSION}; publishing version: $COMPONENT_VERSION"

# Authenticate to ECR and ensure repos exist.
aws ecr get-login-password --region "$PUB_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
aws ecr describe-repositories --repository-names dda/flask-app --region "$PUB_REGION" >/dev/null 2>&1 || \
    aws ecr create-repository --repository-name dda/flask-app --region "$PUB_REGION" >/dev/null
aws ecr describe-repositories --repository-names dda/react-webapp --region "$PUB_REGION" >/dev/null 2>&1 || \
    aws ecr create-repository --repository-name dda/react-webapp --region "$PUB_REGION" >/dev/null

echo "Pushing flask-app to ECR (${ECR_REPO_BACKEND}:${COMPONENT_VERSION})..."
docker tag flask-app:latest "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"
docker push "${ECR_REPO_BACKEND}:${COMPONENT_VERSION}"
echo "Pushing react-webapp to ECR (${ECR_REPO_FRONTEND}:${COMPONENT_VERSION})..."
docker tag react-webapp:latest "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"
docker push "${ECR_REPO_FRONTEND}:${COMPONENT_VERSION}"

# Repackage a scripts-only zip (same name/layout as the full zip, minus the image tars).
rm -f "${STAGE_DIR}/flask-app.tar" "${STAGE_DIR}/react-webapp.tar" "${STAGE_DIR}"/.tmp-* 2>/dev/null || true
APP_ZIP="custom-build/${COMPONENT_NAME}-${ARCH}.zip"
rm -f "$APP_ZIP"
zip -r -X "$APP_ZIP" "custom-build/${COMPONENT_NAME}" -x '*/.tmp-*'

S3_KEY="${COMPONENT_NAME}/${COMPONENT_VERSION}/${COMPONENT_NAME}-${ARCH}.zip"
S3_URI="s3://${S3_BUCKET}/${S3_KEY}"
echo "Uploading scripts artifact to ${S3_URI} ($(numfmt --to=iec "$(stat --format=%s "$APP_ZIP")" 2>/dev/null || echo "?"))..."
aws s3 cp "$APP_ZIP" "$S3_URI" --region "$PUB_REGION"

# Rewrite the recipe: docker image artifacts + S3 scripts artifact, add Docker/TES
# deps, and swap the in-Install `docker load -i ...tar` for `docker tag <ecr> ...:latest`.
python3 -c "import yaml" 2>/dev/null || pip3 install --user pyyaml >/dev/null 2>&1 || true
ECR_RECIPE="greengrass-build/recipes/recipe-ecr.yaml"
mkdir -p greengrass-build/recipes
python3 - "recipe.yaml" "$ECR_RECIPE" "$ECR_REPO_BACKEND" "$ECR_REPO_FRONTEND" "$COMPONENT_VERSION" "$S3_URI" <<'PYEOF'
import re, sys, yaml

src, out, ecr_backend, ecr_frontend, version, s3_uri = sys.argv[1:7]

with open(src) as f:
    recipe = yaml.safe_load(f)

recipe['ComponentVersion'] = version

deps = recipe.setdefault('ComponentDependencies', {})
deps['aws.greengrass.DockerApplicationManager'] = {'VersionRequirement': '~2.0.0'}
deps['aws.greengrass.TokenExchangeService'] = {'VersionRequirement': '~2.0.0'}

def rewrite_install(script: str) -> str:
    script = re.sub(r'docker load -i \S*flask-app\.tar',
                    f'docker tag {ecr_backend}:{version} flask-app:latest', script)
    script = re.sub(r'docker load -i \S*react-webapp\.tar(?:\.gz)?',
                    f'docker tag {ecr_frontend}:{version} react-webapp:latest', script)
    return script

changed = False
for manifest in recipe.get('Manifests', []):
    lifecycle = manifest.get('Lifecycle', {})
    install = lifecycle.get('Install')
    if isinstance(install, dict) and 'Script' in install:
        new_script = rewrite_install(install['Script'])
        if new_script != install['Script']:
            changed = True
        install['Script'] = new_script
    manifest['Artifacts'] = [
        {'URI': f'docker:{ecr_backend}:{version}'},
        {'URI': f'docker:{ecr_frontend}:{version}'},
        {'URI': s3_uri, 'Unarchive': 'ZIP'},
    ]

if not changed:
    sys.stderr.write('ERROR: did not find docker load commands to rewrite in Install; recipe format may have changed.\n')
    sys.exit(2)

with open(out, 'w') as f:
    yaml.dump(recipe, f, default_flow_style=False, sort_keys=False)
print(f'Wrote ECR recipe: {out}')
PYEOF

echo "Creating component version via API..."
aws greengrassv2 create-component-version --inline-recipe fileb://"$ECR_RECIPE" --region "$PUB_REGION"

echo "Tagging component for portal discovery..."
COMPONENT_ARN=$(aws greengrassv2 list-components --scope PRIVATE --region "$PUB_REGION" \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" --output text 2>/dev/null || true)
if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    aws greengrassv2 tag-resource --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" --region "$PUB_REGION" 2>/dev/null \
        && echo "Tagged $COMPONENT_ARN" || echo "WARN: tagging failed (non-critical)"
else
    echo "WARN: could not resolve component ARN for tagging (non-critical)"
fi

echo "DONE: published ${COMPONENT_NAME} v${COMPONENT_VERSION} (ECR + S3)"
