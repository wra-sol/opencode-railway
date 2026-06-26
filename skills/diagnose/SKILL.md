---
name: diagnose
description: Disciplined diagnosis loop for hard bugs and performance regressions. Reproduce, minimise, hypothesise, instrument, fix, regression-test. Use when something is broken, throwing, failing, or when a performance regression appears.
---

# Diagnose: disciplined debugging loop

Use this when something is broken, throwing, failing, or when a performance
regression appears. Do **not** jump straight to a fix. Follow the loop.

## The loop

1. **Reproduce** — Establish a reliable, minimal reproduction. If you cannot
   reproduce it, you cannot know you fixed it. Capture the exact command,
   input, environment, and error output. Save the reproduction as a script or
   test case if possible.

2. **Minimise** — Strip the reproduction down to the smallest trigger. Remove
   inputs, config, and code paths that don't affect the outcome. The smaller
   the repro, the faster the loop and the clearer the cause.

3. **Hypothesise** — Form a specific, falsifiable hypothesis about the cause.
   Write it down: "I believe X because Y. If I change Z, the bug should
   disappear." Vague hypotheses ("maybe a race condition") are not hypotheses.

4. **Instrument** — Add the cheapest probe that tests the hypothesis: a log
   line, a print, a breakpoint, a timing measurement, a debug assertion. Do
   not refactor or "clean up" anything in this step — only observe.

5. **Fix** — Only after the hypothesis is confirmed by instrumentation, make
   the **smallest** change that addresses the root cause. Resist the urge to
   fix adjacent things you noticed along the way — file those separately.

6. **Regression-test** — Run the original reproduction (now passing) and add a
   test that would catch this bug if it returned. Verify you haven't broken
   adjacent behaviour. If the fix doesn't hold under the original repro, you
   are back at step 3.

## Anti-patterns to avoid

- **Shotgun debugging** — changing multiple things at once and hoping one
  works. You lose the ability to attribute the fix.
- **Fixing symptoms** — patching the error message or the downstream effect
  without understanding why the upstream condition occurred.
- **Skipping reproduction** — "I know what the problem is" without a repro.
  You will not be able to verify the fix.
- **Big fixes for small bugs** — if the fix is larger than the bug, reconsider.
  A one-line fix backed by instrumentation beats a 50-line rewrite backed by
  intuition.

## When to stop

The loop is done when: the reproduction passes, a regression test exists, the
hypothesis is confirmed, and no new failures appeared in adjacent behaviour.
If any of those are false, you are still in the loop.
