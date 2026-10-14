#!/usr/bin/env bash
# Creates a Google Cloud project with a service account for the bot and saves
# its key as service-account.json. Needs the Google Cloud CLI (gcloud).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "Install the Google Cloud CLI first."
  echo "  On a Mac with Homebrew: brew install --cask gcloud-cli"
  echo "  Otherwise: https://cloud.google.com/sdk/docs/install"
  exit 1
fi
if [ -f service-account.json ]; then
  echo "service-account.json already exists. Delete it first if you want a new one."
  exit 1
fi

if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)')" ]; then
  echo "A browser window will open. Sign in with the Google account that will own the Sheet."
  gcloud auth login
fi

project="campaign-bot-$(openssl rand -hex 3)"
echo "Creating Google Cloud project $project..."
gcloud projects create "$project" --name="Campaign bot"
gcloud services enable sheets.googleapis.com drive.googleapis.com docs.googleapis.com --project "$project"
gcloud iam service-accounts create campaign-bot --display-name="Campaign bot" --project "$project"
email="campaign-bot@$project.iam.gserviceaccount.com"
gcloud iam service-accounts keys create service-account.json --iam-account "$email" --project "$project"
chmod 600 service-account.json

echo
echo "Done. The bot's Google account is: $email"
echo "Share your Google Sheet and docs with it as Editor, then run ./setup.sh"
