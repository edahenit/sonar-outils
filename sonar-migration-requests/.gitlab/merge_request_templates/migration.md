## SonarQube migration request

- **Source instance**:
- **Source key**:
- **Target key**:
- **Associated ticket** (optional):

### Before submitting

- [ ] I have verified that I am an administrator of the **source**
      project on my entity's SonarQube instance.
- [ ] I have verified that I am an administrator of the **target**
      project on the central instance (created by the DevOps portal for
      my space).
- [ ] The added file is at the correct location:
      `requests/<instance_source>/<cle_cible>.yml`.
- [ ] I have not modified any file other than my request in this MR.

### What happens next

1. The merge to `main` automatically triggers an authorization check and
   preflight checks. The verdict is published as a comment on this MR
   within a few minutes.
2. If the check fails, no action is taken on any instance: fix the issue
   per the message received and submit a new request.
3. If the check succeeds, the central team schedules and triggers
   execution within a migration window agreed with you.
