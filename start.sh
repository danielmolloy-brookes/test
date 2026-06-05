#!/bin/bash
# Smart Event Check-In — Quick Start Script

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Smart Event Check-In — Starting    ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check .env exists
if [ ! -f ".env" ]; then
  echo "⚠️  No .env file found. Copying from .env.example..."
  cp .env.example .env
  echo "✅ Created .env — please edit it with your GHL credentials."
  echo ""
fi

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 not found. Please install Python 3.10+."
  exit 1
fi

# Create/activate venv
if [ ! -d "venv" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt -q

# Create dirs
mkdir -p static/qr_codes data

# Start app
echo ""
echo "🚀 Starting server on http://localhost:8000"
echo "   Username: $(grep ADMIN_USERNAME .env | cut -d= -f2)"
echo "   Password: (see .env → ADMIN_PASSWORD)"
echo ""
echo "   Press Ctrl+C to stop."
echo ""

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
