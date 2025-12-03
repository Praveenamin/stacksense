# Maintaining Your StackSense Repository

## ✅ Your Code is Now on GitHub!

Repository: https://github.com/Praveenamin/stacksense

## 📋 Daily Workflow

### Making Changes

1. **Make your changes** to files in the repository
2. **Stage changes**:
   ```bash
   cd /home/ubuntu/stacksense-repo
   git add .
   ```
3. **Commit changes**:
   ```bash
   git commit -m "Description of your changes"
   ```
4. **Push to GitHub**:
   ```bash
   git push origin main
   ```

### Example Workflow

```bash
# After making changes to monitoring_dashboard.html
cd /home/ubuntu/stacksense-repo
git add .
git commit -m "Update admin users modal styling"
git push origin main
```

## 🔄 Syncing with Docker Container

Since your running application is in the Docker container, you may need to sync changes:

### Option 1: Copy files to container after changes
```bash
# After committing changes locally
docker cp /home/ubuntu/stacksense-repo/core/templates/core/monitoring_dashboard.html monitoring_web:/app/core/templates/core/
docker restart monitoring_web
```

### Option 2: Pull from Git inside container (if mounted)
If your `/app` directory is a volume mount, you can pull directly:
```bash
docker exec monitoring_web git pull origin main
```

## 🌿 Branch Management

### Create a Feature Branch
```bash
git checkout -b feature/new-feature
# Make changes
git add .
git commit -m "Add new feature"
git push origin feature/new-feature
```

### Switch Back to Main
```bash
git checkout main
```

### Merge Feature Branch
```bash
git checkout main
git merge feature/new-feature
git push origin main
```

## 📝 Good Commit Messages

Write clear, descriptive commit messages:

✅ **Good:**
- "Add scrollbar to disk storage card"
- "Implement admin users modal dialog"
- "Fix login page visibility issue"
- "Update design system colors to gold theme"

❌ **Bad:**
- "fix"
- "update"
- "changes"
- "asdf"

## 🔍 Checking Status

```bash
# See what files changed
git status

# See detailed changes
git diff

# See commit history
git log --oneline

# See remote status
git remote -v
```

## 🔄 Pulling Latest Changes

If working from multiple machines:

```bash
git pull origin main
```

## 🏷️ Tagging Releases

Tag important versions:

```bash
git tag -a v1.0.0 -m "Initial release"
git push origin v1.0.0
```

## 🚨 Troubleshooting

### Undo Last Commit (keep changes)
```bash
git reset --soft HEAD~1
```

### Undo Last Commit (discard changes)
```bash
git reset --hard HEAD~1
```

### Revert a File
```bash
git checkout -- filename
```

### Force Push (use with caution!)
```bash
git push origin main --force
```

## 📚 Resources

- GitHub Repository: https://github.com/Praveenamin/stacksense
- Git Documentation: https://git-scm.com/doc
- GitHub Guides: https://guides.github.com

## 🔐 Security Reminders

- ✅ Never commit `.env` files
- ✅ Never commit SSH private keys
- ✅ Never commit database files
- ✅ Always review `git status` before committing
- ✅ Use descriptive commit messages

## 🎯 Best Practices

1. **Commit often** - Small, frequent commits are better
2. **Test before pushing** - Make sure code works
3. **Write clear messages** - Help future you understand changes
4. **Review changes** - Use `git diff` before committing
5. **Keep main branch stable** - Use feature branches for experiments







