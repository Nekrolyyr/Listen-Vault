-- Onboarding (docs/gaps-plan.md, phase 6): a new account goes through it once. Accounts that exist now are
-- treated as onboarded, so the single-user library is not walked through it.
ALTER TABLE users ADD COLUMN onboarded_at timestamptz;
UPDATE users SET onboarded_at = now();
