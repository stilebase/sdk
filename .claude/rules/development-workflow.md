# How changes are built

Every change to this repository (code, docs, CI) follows these steps. They're loaded into every Claude Code session
automatically; people follow them too.

## 1. A branch, never `main`

Branch from an up-to-date `main` (`git fetch` and fast-forward first) with a type prefix: `feature/` (new behaviour),
`bugfix/`, `hotfix/` (an urgent fix to a published release), `refactor/`, `docs/`, `chore/` (tooling, dependencies,
CI), `test/`. Then a short kebab-case name: `feature/sdk-js-fastify`, `bugfix/py-redirects`.

**Never commit or push to `main` directly.** No exceptions: not for a typo, a version bump, a revert or a hotfix.
Never force-push `main`. Changes reach `main` only by squash-merging a pull request whose CI checks are all green; a
ruleset enforces it. Before the first commit, check the branch (`git branch --show-current`).

One package per pull request: a change to `sdk-js` and one to `sdk-python` are two pull requests, except when they
must land together (a conformance change both have to pass).

## 2. Plan first

For anything beyond a small, obvious fix, write a plan and get the user's approval before building.

## 3. Two or three cycles of build, test, review

Each cycle is:

1. **Build.** Implement, or fix what the previous review found.
2. **Test.** The package's checks (CLAUDE.md, "Build & test"), smoke test included. Everything must be green, and new
   behaviour must come with tests.
3. **Review.** Run `/code-review xhigh` on the branch against `main`. Fix every confirmed finding. For a finding you
   don't fix, write down why.

Run at least two cycles and at most three. From cycle 2 on, a review with no **blocking** findings ends the loop. A
finding is blocking when:

- it breaks a rule in CLAUDE.md, "Rules the gate keeps";
- it lets an agent read without paying, or breaks the site the gate protects;
- it changes behaviour for sites already running a published version; or
- it can lose money or data.

Non-blocking findings go on `TODO.md`, each with a line on why it waits.

## 4. When it doesn't converge, ask

Stop and tell the user before going on if a fourth cycle would be needed, or a finding changes the agreed scope or
design. Tell them what's left, why, the options, and your recommendation. Don't continue without their answer.

## 5. Merge only when green

Push the branch and open a pull request. Squash-merge it only when every CI check on it is green and the last review
is clean. A red or still-running check means no merge: fix it on the branch and push again.

Merging doesn't publish anything; a release tag does (CLAUDE.md, "Releases"). Say in the pull request what changes for
sites using the package once it's released.

## 6. Report

At the end, tell the user: how many cycles ran, the findings fixed and not fixed (with why), the test results, the
pull request link, and anything they have to do themselves.
