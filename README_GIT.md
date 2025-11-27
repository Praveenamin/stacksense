# StackSense - Git Repository

## 🚀 Quick Start

This repository contains the StackSense Server Monitoring System.

### First Time Push

1. **Get a Personal Access Token (PAT)**:
   - Go to: https://github.com/settings/tokens
   - Click "Generate new token (classic)"
   - Select `repo` scope
   - Copy the token

2. **Push to GitHub**:
   ```bash
   cd /home/ubuntu/stacksense-repo
   ./push-to-github.sh
   ```
   
   OR manually:
   ```bash
   git push origin main
   # Username: Praveenamin
   # Password: [paste your PAT]
   ```

### Repository Structure

```
stacksense-repo/
├── core/                    # Main Django app
│   ├── management/         # Management commands
│   ├── migrations/          # Database migrations
│   ├── static/             # Static files (CSS, images)
│   ├── templates/          # HTML templates
│   └── ...
├── log_analyzer/           # Django project settings
├── requirements.txt        # Python dependencies
├── .env.example           # Environment variables template
├── .gitignore             # Git ignore rules
└── README.md              # Project documentation
```

### Security

✅ **Protected Files** (never committed):
- `.env` - Environment variables
- `ssh_keys/id_rsa` - SSH private keys
- `*.log` - Log files
- `__pycache__/` - Python cache
- Database files

### Daily Workflow

```bash
# Make changes
cd /home/ubuntu/stacksense-repo

# Stage changes
git add .

# Commit
git commit -m "Your commit message"

# Push
git push origin main
```

### Branch Management

- `main` - Production-ready code
- Create feature branches for new features:
  ```bash
  git checkout -b feature/new-feature
  git push origin feature/new-feature
  ```

## 📚 Documentation

- See `README.md` for project documentation
- See `PUSH_INSTRUCTIONS.md` for detailed push instructions
- See `START_HERE.txt` for setup instructions

## 🔗 Links

- Repository: https://github.com/Praveenamin/stacksense
- Issues: https://github.com/Praveenamin/stacksense/issues
