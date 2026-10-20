# Continuity Ledger

## Goal
Push the current project (`youtube-viet-dub`) to GitHub.

## Constraints & Assumptions
- Git is installed. Default branch configured to `main`.
- User identity: `phamngocan2412` <an.207ct68609@gmail.com>.
- GitHub Account: `An2412` (SSH authentication confirmed working via `ssh -T git@github.com`).
- Remote GitHub repository: `trans-vietsub` (`git@github.com:An2412/trans-vietsub.git` / `https://github.com/An2412/trans-vietsub.git`).

## Key Decisions
- Initialize git repository locally with default branch `main`.
- Stage all project files adhering to `.gitignore`.
- Create initial commit.
- Use SSH URL `git@github.com:An2412/trans-vietsub.git` so git push succeeds seamlessly with user's verified SSH key.

## Progress State
- Done:
  - Inspected repository structure and `.gitignore`.
  - Checked git configuration and confirmed SSH key access.
  - Initialized git repository on branch `main`.
  - Staged and created initial commit.
  - Added remote origin (`git@github.com:An2412/trans-vietsub.git`).
  - Fetched and merged remote `origin/main` (incorporating `LICENSE` cleanly).
  - Pushed branch `main` to GitHub (`git push -u origin main`) successfully.
  - Verified Windows compatibility: codebase already includes `install.bat`, `start-server.bat`, Windows Credential Manager support in `provider_settings.py`, and multi-platform `requirements.txt`.
- Now:
  - Fixed CC audio pause-and-resume stuttering issue:
    - Analyzed root causes: hard video pause when nextCue.start <= now + 0.25, single-worker starvation (MAX_PARALLEL = 1), deadlock in resumeIfReady() across sentence gaps (>0.5s), and zero tolerance on cue due timings.
    - Implemented Adaptive Speech Rate Scaling algorithm (clamped between 0.95x and 1.35x of video rate) to automatically fit spoken Vietnamese audio inside the CC duration window with pitch preservation.
    - Implemented audio preloading and zero-delay chained playback between cues.
    - Increased prefetch parallelism to 3 concurrent workers and added a 0.4s grace period before video pause.
    - Added natural pause segmentation (gap >= 0.65s) in buildCues to avoid overly long run-on captions.
- Next:
  - Test playback on YouTube with live extension.
