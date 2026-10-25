#!/usr/bin/env bash
# Day-1 account setup (ARCHITECTURE.md §5). Run by the ACCOUNT OWNER, with admin credentials,
# after reading every command. Nothing here is run automatically by the finops tool.
#
#   ACCOUNT_ID=123456789012 IAM_USER=alice ALERT_EMAIL=you@example.com ./iam/setup.sh
set -euo pipefail
: "${ACCOUNT_ID:?}" "${IAM_USER:?}" "${ALERT_EMAIL:?}"
cd "$(dirname "$0")"
TRUST=$(sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/" -e "s/YOUR_IAM_USER/$IAM_USER/" trust-policy.json)

aws iam create-role --role-name finops-readonly --assume-role-policy-document "$TRUST" \
  --max-session-duration 3600
aws iam put-role-policy --role-name finops-readonly --policy-name finops-readonly \
  --policy-document file://readonly-policy.json

aws iam create-role --role-name finops-remediator --assume-role-policy-document "$TRUST" \
  --max-session-duration 3600
aws iam put-role-policy --role-name finops-remediator --policy-name finops-remediator \
  --policy-document file://remediator-policy.json

# Budget alerts at $5 (actual) and $20 (forecast)
for spec in "5:ACTUAL" "20:FORECASTED"; do
  amount=${spec%%:*}; kind=${spec##*:}
  aws budgets create-budget --account-id "$ACCOUNT_ID" \
    --budget "{\"BudgetName\":\"finops-safety-$amount\",\"BudgetLimit\":{\"Amount\":\"$amount\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}" \
    --notifications-with-subscribers "[{\"Notification\":{\"NotificationType\":\"$kind\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]}]"
done

cat <<MSG
Done. Add to ~/.aws/config (source_profile = the profile holding $IAM_USER's keys):

[profile finops-ro]
role_arn = arn:aws:iam::$ACCOUNT_ID:role/finops-readonly
source_profile = default
mfa_serial = arn:aws:iam::$ACCOUNT_ID:mfa/$IAM_USER
region = ap-south-1

[profile finops-rw]
role_arn = arn:aws:iam::$ACCOUNT_ID:role/finops-remediator
source_profile = default
mfa_serial = arn:aws:iam::$ACCOUNT_ID:mfa/$IAM_USER
region = ap-south-1

Then: finops whoami
Optional: opt in to Compute Optimizer:  aws compute-optimizer update-enrollment-status --status Active
MSG
