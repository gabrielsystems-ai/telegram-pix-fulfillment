# Independent GitHub publication

The local repository has no remote. Publication is intentionally deferred until owner review.

1. Read `docs/VERIFICATION.md`, `SECURITY.md` and `THIRD_PARTY_NOTICES.md`. Review the tracked files, secret scan and known operational limitations.
2. Run the documented local checks again. Confirm `.env`, media, local databases and caches are ignored. Review `git status --short` and `git ls-files`.
3. Confirm your authorship/distribution rights for the project source. The MIT license covers this project; dependency notices retain their own terms.
4. Create a new **empty** repository named `telegram-pix-fulfillment` on GitHub. Do not initialize it with a README, license or `.gitignore`. Choose public visibility only after reviewing the actual staged tree.
5. In this independent project directory, create the initial commit if it has not been created, then connect only the new GitHub URL:

```powershell
git add .
git diff --cached --stat
git diff --cached
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/telegram-pix-fulfillment.git
git remote -v
git push -u origin main
```

Replace `YOUR_ACCOUNT` with your GitHub account. Configure the author identity you want to publish before committing. No existing commercial repository or remote is needed. The commands above are instructions for your reviewed publication step; they are not run automatically.

GitHub Actions can run the isolated checks from `.github/workflows/ci.yml` without repository secrets. Enable secret scanning/push protection where available. Never upload a working `.env`, runtime logs, customer database, media cache, real Pix payload or private diagnostic artifact.
