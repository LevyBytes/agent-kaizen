<!-- See CONTRIBUTING.md. This template follows SAVMI: Scope, Adapt, Verify, Manage, Improve. -->

## Summary

<!-- Scope: what this changes and why, in one or two sentences. -->

## Scope

<!-- Which files or surfaces this touches. Link a related issue if there is one. -->

## Change and compatibility

<!-- Adapt: the user-visible contract (behavior, flags, output) and any compatibility impact. -->

## Testing

<!-- Verify: the exact command you ran and its outcome. CI on your current commit is authoritative. -->

```text
command  # PASS/FAIL
```

## Risks, rollback, and unresolved concerns

<!-- Manage: known risks, how to roll this back, and any concern you have not resolved. -->

## Improve and checklist

<!-- Improve: note the regression protection and documentation updates, or explain why they do not apply. -->

- [ ] The change is focused; unrelated changes are in separate PRs.
- [ ] All commits are signed off (`git commit -s`) per the DCO.
- [ ] Behavior changes have a new or updated test (or an explanation why not).
- [ ] Docs updated, or not applicable.
- [ ] This PR changes a dependency, workflow, installer, policy, schema, validator, test-runner, or security surface. <!-- check if true; expect a manual diff review -->
- [ ] No secrets, credentials, personal paths, or machine names are included.
