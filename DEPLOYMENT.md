# CodeReviewAgent - Deployment Guide

This guide covers both **local development setup** and **production deployment** with GitLab CI/CD integration.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Development Setup](#local-development-setup)
  - [1. Clone the Repository](#1-clone-the-repository)
  - [2. Environment Configuration](#2-environment-configuration)
  - [3. Setup MCP Servers](#3-setup-mcp-servers)
  - [4. Run with Docker Compose](#4-run-with-docker-compose)
  - [5. Test the Setup](#5-test-the-setup)
- [Production Deployment](#production-deployment)
  - [1. Server Requirements](#1-server-requirements)
  - [2. Production Environment Setup](#2-production-environment-setup)
  - [3. Deploy with Docker](#3-deploy-with-docker)
  - [4. Health Monitoring](#4-health-monitoring)
- [GitLab CI/CD Integration](#gitlab-cicd-integration)
  - [Option A: Post-Pipeline Review (Recommended)](#option-a-post-pipeline-review-recommended)
  - [Option B: Webhook-Based Review](#option-b-webhook-based-review)
  - [Option C: Manual Trigger](#option-c-manual-trigger)
- [Troubleshooting](#troubleshooting)
- [Security Best Practices](#security-best-practices)

---

## Prerequisites

### Required
- **Docker** and **Docker Compose** (v20.10 or higher)
- **GitLab Personal Access Token** with `api`, `read_api`, and `write_repository` scopes
- **OpenAI API Key** for AI-powered code reviews

### Optional (for enhanced functionality)
- **Jira Personal Access Token** (for task description integration)
- **Confluence Personal Access Token** (for documentation lookup)
- **Corporate Proxy** settings (if behind a firewall)

---

## Local Development Setup

### 1. Clone the Repository

```bash
git clone https://gitlab.example.com/AmirHosseinHa/codereviewagent.git
cd codereviewagent
```

### 2. Environment Configuration

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# ============================================
# REQUIRED CONFIGURATION
# ============================================

# OpenAI API Key (Required for AI reviews)
OPENAI_API_KEY=sk-your_openai_api_key_here

# GitLab Configuration (Required)
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=***REMOVED***

# ============================================
# OPTIONAL CONFIGURATION
# ============================================

# Proxy Configuration (if behind corporate firewall)
HTTP_PROXY=http://proxy.example.com:8080
HTTPS_PROXY=http://proxy.example.com:8080
PROXY_USERNAME=your_proxy_username
PROXY_PASSWORD=your_proxy_password

# Jira Configuration (for enhanced task context)
JIRA_URL=http://jira.example.com
JIRA_PERSONAL_ACCESS_TOKEN=your_jira_pat_here

# Confluence Configuration (for documentation lookup)
CONFLUENCE_URL=http://confluence.example.com/
CONFLUENCE_PERSONAL_ACCESS_TOKEN=your_confluence_pat_here

# ============================================
# MCP SERVER CONFIGURATION
# ============================================

# MCP Atlassian (Jira/Confluence integration)
MCP_ATLASSIAN_ENABLED=true
MCP_ATLASSIAN_HOST=localhost
MCP_ATLASSIAN_PORT=3000
MCP_ATLASSIAN_CONTAINER=mcp-atlassian
MCP_ATLASSIAN_SSH_USER=root

# MCP GitLab (Required for GitLab operations)
MCP_GITLAB_ENABLED=true
MCP_GITLAB_HOST=localhost
MCP_GITLAB_CONTAINER=mcp-gitlab
MCP_GITLAB_SSH_USER=root
```

### 3. Setup MCP Servers

The Code Review Agent requires MCP (Model Context Protocol) servers for GitLab and Atlassian integrations. These are automatically started with Docker Compose.

**MCP Servers Used:**
- **GitLab MCP**: `iwakitakuma/gitlab-mcp` (for GitLab API operations)
- **Atlassian MCP**: `ghcr.io/sooperset/mcp-atlassian:latest` (for Jira/Confluence)

### 4. Run with Docker Compose

Start all services (MCP servers + CodeReviewAgent):

```bash
# Build and start all containers
docker-compose up -d --build

# View logs
docker-compose logs -f

# Check running containers
docker-compose ps
```

This will start:
- **mcp-atlassian**: Jira/Confluence MCP server (port 3000)
- **mcp-gitlab**: GitLab MCP server
- **codereviewagent**: Main application (port 5000)

### 5. Test the Setup

#### Option A: Test via Web UI

1. Open browser: `http://localhost:5000`
2. Enter a merge request URL
3. Click "Review" to start code review

#### Option B: Test via Command Line

```bash
# Review a merge request
docker exec -it codereviewagent python main.py \
  --gitlab-mr "https://gitlab.example.com/group/project/-/merge_requests/123"
```

#### Option C: Test via API

```bash
curl -X POST http://localhost:5000/api/review \
  -H "Content-Type: application/json" \
  -d '{"mr": "https://gitlab.example.com/group/project/-/merge_requests/123"}'
```

---

## Production Deployment

### 1. Server Requirements

**Minimum Requirements:**
- **CPU**: 2 cores
- **RAM**: 4 GB
- **Disk**: 20 GB
- **OS**: Ubuntu 20.04+ or any Linux with Docker support
- **Network**: Outbound access to OpenAI API, GitLab, Jira, Confluence

**Recommended for Production:**
- **CPU**: 4+ cores
- **RAM**: 8 GB
- **Disk**: 50 GB SSD
- **Network**: Static IP or internal DNS name

### 2. Production Environment Setup

On your production server:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Create application directory
sudo mkdir -p /opt/codereviewagent
cd /opt/codereviewagent

# Clone repository
git clone https://gitlab.example.com/AmirHosseinHa/codereviewagent.git .

# Create .env file with production credentials
sudo nano .env
```

**Production `.env` Template:**

```env
OPENAI_API_KEY=your_production_openai_key
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=your_production_gitlab_token

# Production proxy (if needed)
HTTP_PROXY=http://internal.example.com:8080
HTTPS_PROXY=http://internal.example.com:8080

# Jira/Confluence (optional)
JIRA_URL=http://jira.example.com
JIRA_PERSONAL_ACCESS_TOKEN=your_jira_token
CONFLUENCE_URL=http://confluence.example.com/
CONFLUENCE_PERSONAL_ACCESS_TOKEN=your_confluence_token

# MCP Configuration
MCP_ATLASSIAN_ENABLED=true
MCP_ATLASSIAN_HOST=localhost
MCP_ATLASSIAN_PORT=3000
MCP_GITLAB_ENABLED=true
MCP_GITLAB_HOST=localhost
```

### 3. Deploy with Docker

```bash
# Build and start services
sudo docker-compose up -d --build

# Enable auto-restart on server reboot
sudo docker-compose restart

# Check logs
sudo docker-compose logs -f codereviewagent
```

**Configure as System Service (Optional):**

Create `/etc/systemd/system/codereviewagent.service`:

```ini
[Unit]
Description=CodeReviewAgent Service
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/codereviewagent
ExecStart=/usr/bin/docker-compose up -d
ExecStop=/usr/bin/docker-compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable codereviewagent
sudo systemctl start codereviewagent
```

### 4. Health Monitoring

**Check Service Status:**

```bash
# Container status
docker-compose ps

# View logs
docker-compose logs --tail=100 -f

# Check resource usage
docker stats
```

**Setup Monitoring (Optional):**

```bash
# Add health check endpoint monitoring
curl http://localhost:5000/

# Setup log rotation
sudo nano /etc/logrotate.d/codereviewagent
```

---

## GitLab CI/CD Integration

This section explains how to integrate the Code Review Agent into your GitLab CI/CD pipeline.

### Option A: Post-Pipeline Review (Recommended)

This option triggers code review **after all existing pipeline stages pass**.

#### Step 1: Create `.gitlab-ci.yml` Stage

Add a new stage to your existing `.gitlab-ci.yml`:

```yaml
stages:
  - build
  - test
  - deploy
  - code-review  # New stage - runs after all others pass

# ... your existing jobs ...

# Code Review Job (runs after all stages pass)
code-review:
  stage: code-review
  image: curlimages/curl:latest
  only:
    - merge_requests  # Only run on merge requests
  script:
    - |
      echo "🚀 Starting AI Code Review..."
      RESPONSE=$(curl -X POST "${CODE_REVIEW_API_URL}/api/review" \
        -H "Content-Type: application/json" \
        -d "{\"mr\": \"${CI_MERGE_REQUEST_PROJECT_URL}/-/merge_requests/${CI_MERGE_REQUEST_IID}\"}" \
        -w "\n%{http_code}" -s)
      
      HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
      BODY=$(echo "$RESPONSE" | head -n-1)
      
      echo "Response: $BODY"
      echo "HTTP Code: $HTTP_CODE"
      
      if [ "$HTTP_CODE" -ne "200" ]; then
        echo "❌ Code review failed with HTTP $HTTP_CODE"
        exit 1
      fi
      
      echo "✅ Code review completed successfully"
  allow_failure: true  # Don't block merge if review fails
  variables:
    CODE_REVIEW_API_URL: "http://your-codereview-server:5000"
```

#### Step 2: Configure GitLab Variables

In your GitLab project:

1. Go to **Settings → CI/CD → Variables**
2. Add the following variables:

| Variable | Value | Protected | Masked |
|----------|-------|-----------|--------|
| `CODE_REVIEW_API_URL` | `http://your-server:5000` | ✅ | ❌ |

#### Step 3: Test the Integration

1. Create a merge request
2. Watch the pipeline run through all stages
3. The `code-review` stage will trigger last
4. Check MR comments for review results

---

### Option B: Webhook-Based Review

This option uses GitLab webhooks to trigger reviews immediately when MRs are created/updated.

#### Step 1: Setup Webhook in GitLab

1. Go to **Project Settings → Webhooks**
2. Add new webhook:
   - **URL**: `http://your-codereview-server:5000/api/review`
   - **Trigger**: ✅ Merge request events
   - **Enable SSL verification**: Configure based on your setup
3. Click **Add webhook**

#### Step 2: Update `app.py` for Webhook Support

Modify `/home/amirhossein/projects/codereviewagent/app.py`:

```python
@app.route('/api/review', methods=['POST'])
def review():
    data = request.get_json()
    
    # Support both direct API calls and GitLab webhooks
    if 'object_attributes' in data:
        # GitLab webhook payload
        mr_iid = data['object_attributes']['iid']
        project_url = data['project']['web_url']
        mr_url = f"{project_url}/-/merge_requests/{mr_iid}"
    else:
        # Direct API call
        mr_url = data.get('mr')
    
    if not mr_url:
        return jsonify({'error': 'MR URL is required'}), 400
    
    # Run review asynchronously
    asyncio.run(process_mr(mr_url))
    return jsonify({'result': 'DONE', 'mr': mr_url})
```

#### Step 3: Test Webhook

```bash
# Test webhook manually
curl -X POST http://your-server:5000/api/review \
  -H "Content-Type: application/json" \
  -d @webhook-test-payload.json
```

---

### Option C: Manual Trigger

Run code review manually via GitLab CI job with manual trigger.

```yaml
# Add to .gitlab-ci.yml
code-review-manual:
  stage: code-review
  image: curlimages/curl:latest
  when: manual  # Manual trigger only
  only:
    - merge_requests
  script:
    - |
      curl -X POST "${CODE_REVIEW_API_URL}/api/review" \
        -H "Content-Type: application/json" \
        -d "{\"mr\": \"${CI_MERGE_REQUEST_PROJECT_URL}/-/merge_requests/${CI_MERGE_REQUEST_IID}\"}"
  variables:
    CODE_REVIEW_API_URL: "http://your-server:5000"
```

---

## Troubleshooting

### Common Issues

#### 1. **OpenAI API Connection Failed**

```bash
# Check proxy settings
docker exec -it codereviewagent env | grep PROXY

# Test OpenAI connectivity
docker exec -it codereviewagent curl -I https://api.openai.com
```

**Solution**: Verify `OPENAI_API_KEY` and proxy settings in `.env`.

---

#### 2. **GitLab MCP Server Not Responding**

```bash
# Check MCP GitLab container
docker logs mcp-gitlab

# Restart MCP server
docker-compose restart mcp-gitlab
```

**Solution**: Ensure `GITLAB_TOKEN` has proper permissions (`api`, `read_api`).

---

#### 3. **Cannot Post Comments to MR**

**Common Causes:**
- Insufficient GitLab token permissions
- MCP server connection issues
- Network/firewall blocking

**Solution:**
```bash
# Verify GitLab token scopes
curl -H "PRIVATE-TOKEN: your_token" \
  https://gitlab.example.com/api/v4/personal_access_tokens/self

# Check MCP connectivity
docker exec -it codereviewagent ping mcp-gitlab
```

---

#### 4. **Container Won't Start**

```bash
# Check container logs
docker-compose logs codereviewagent

# Rebuild from scratch
docker-compose down -v
docker-compose up -d --build
```

---

#### 5. **Pipeline Integration Not Working**

**Check:**
- CI/CD variable `CODE_REVIEW_API_URL` is set correctly
- Network connectivity from GitLab Runner to Code Review server
- Firewall rules allow traffic on port 5000

```bash
# Test from GitLab Runner host
curl http://your-codereview-server:5000/
```

---

## Security Best Practices

### 1. **Protect Sensitive Tokens**

- ✅ Store all tokens in `.env` file (never commit to Git)
- ✅ Use GitLab CI/CD **masked variables** for tokens
- ✅ Rotate tokens regularly (every 90 days)
- ❌ Never hardcode tokens in source code

### 2. **Network Security**

```bash
# Use internal network for MCP servers
# Update docker-compose.yml
networks:
  codereview-network:
    driver: bridge
    internal: true  # No external access
```

### 3. **Access Control**

- Limit GitLab token scope to minimum required permissions
- Use project-specific tokens instead of admin tokens
- Enable IP whitelisting if possible

### 4. **HTTPS/TLS**

For production, use a reverse proxy with SSL:

```bash
# Example: Nginx reverse proxy
sudo apt install nginx
sudo nano /etc/nginx/sites-available/codereview
```

**Nginx Config:**

```nginx
server {
    listen 443 ssl;
    server_name internal.example.com;

    ssl_certificate /etc/ssl/certs/codereview.crt;
    ssl_certificate_key /etc/ssl/private/codereview.key;

    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 5. **Logging and Auditing**

```bash
# Enable structured logging
docker-compose logs --tail=1000 > /var/log/codereview/app.log

# Setup log rotation
sudo nano /etc/logrotate.d/codereview
```

---

## Architecture Overview

```
┌─────────────────┐         ┌──────────────────┐
│   GitLab MR     │────────▶│  GitLab CI/CD    │
│   (Developer)   │         │   Pipeline       │
└─────────────────┘         └────────┬─────────┘
                                     │
                                     │ Trigger after tests pass
                                     ▼
                            ┌─────────────────────┐
                            │ CodeReviewAgent API  │
                            │   (Flask App)        │
                            └──────────┬──────────┘
                                       │
                     ┌─────────────────┼─────────────────┐
                     │                 │                 │
                     ▼                 ▼                 ▼
            ┌────────────────┐ ┌─────────────┐ ┌──────────────┐
            │ MCP GitLab     │ │ MCP Jira    │ │ OpenAI API   │
            │ (GitLab Ops)   │ │ (Context)   │ │ (AI Review)  │
            └────────────────┘ └─────────────┘ └──────────────┘
                     │
                     ▼
            ┌────────────────┐
            │ Review Comments│
            │ Posted to MR   │
            └────────────────┘
```

---

## Support

For issues or questions:

1. Check [Troubleshooting](#troubleshooting) section
2. Review application logs: `docker-compose logs -f`
3. Open issue in GitLab repository
4. Contact: AmirHossein (gitlab.example.com/AmirHosseinHa)

---

## License

[Add your license here]

---

**Last Updated**: January 2026

