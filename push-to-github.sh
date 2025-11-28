#!/bin/bash

# StackSense - Push to GitHub Script
# This script helps you push your code to GitHub using a Personal Access Token

echo "=========================================="
echo "StackSense - Push to GitHub"
echo "=========================================="
echo ""

# Check if we're in the right directory
if [ ! -d ".git" ]; then
    echo "❌ Error: Not a Git repository"
    echo "Please run this script from the repository root: /home/ubuntu/stacksense-repo"
    exit 1
fi

# Check if remote is configured
if ! git remote get-url origin > /dev/null 2>&1; then
    echo "❌ Error: Remote repository not configured"
    exit 1
fi

echo "Repository: $(git remote get-url origin)"
echo "Branch: $(git branch --show-current)"
echo ""

# Check if there are uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    echo "⚠️  Warning: You have uncommitted changes"
    echo ""
    read -p "Do you want to commit them first? (y/n): " commit_choice
    if [ "$commit_choice" = "y" ]; then
        read -p "Enter commit message: " commit_msg
        git add .
        git commit -m "$commit_msg"
    fi
fi

echo ""
echo "To push to GitHub, you need a Personal Access Token (PAT)"
echo ""
echo "If you don't have one:"
echo "1. Go to: https://github.com/settings/tokens"
echo "2. Generate new token (classic)"
echo "3. Select 'repo' scope"
echo "4. Copy the token"
echo ""

read -p "Do you have a PAT ready? (y/n): " has_pat

if [ "$has_pat" != "y" ]; then
    echo ""
    echo "Please create a PAT first, then run this script again."
    exit 0
fi

echo ""
echo "Choose push method:"
echo "1. Push with PAT in URL (one-time, less secure)"
echo "2. Push with credential prompt (recommended)"
echo "3. Set up credential helper (saves PAT for future)"
read -p "Enter choice (1-3): " push_method

case $push_method in
    1)
        read -sp "Enter your PAT: " pat
        echo ""
        git push https://${pat}@github.com/Praveenamin/stacksense.git main
        ;;
    2)
        echo ""
        echo "When prompted:"
        echo "  Username: Praveenamin"
        echo "  Password: [paste your PAT here]"
        echo ""
        read -p "Press Enter to continue..."
        git push origin main
        ;;
    3)
        echo ""
        echo "Setting up credential helper..."
        git config credential.helper store
        echo ""
        echo "When prompted:"
        echo "  Username: Praveenamin"
        echo "  Password: [paste your PAT here]"
        echo ""
        read -p "Press Enter to continue..."
        git push origin main
        echo ""
        echo "✅ Credential helper configured. Future pushes won't require PAT entry."
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Successfully pushed to GitHub!"
    echo ""
    echo "View your repository at:"
    echo "https://github.com/Praveenamin/stacksense"
else
    echo ""
    echo "❌ Push failed. Please check:"
    echo "1. Your PAT is valid and has 'repo' scope"
    echo "2. The repository exists and you have write access"
    echo "3. Your internet connection is working"
fi


