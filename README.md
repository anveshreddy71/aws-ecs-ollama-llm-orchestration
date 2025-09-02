# aws-ecs-ollama-llm-orchestration

Decoupled architecture for running LLMs on AWS ECS with Ollama. FastAPI master app on Fargate orchestrates GPU executors on EC2, enabling secure, scalable, and cost-efficient inference with dynamic GPU provisioning and Service Connect networking.

# Running Locally

Follow these steps to set up and run the application on your local machine:

## 1. Install Python

Download and install Python (version 3.8 or higher) from [https://www.python.org/downloads/](https://www.python.org/downloads/).

## 2. Create a Virtual Environment

Open your terminal in the project directory and run:
```
python -m venv env_selfhost
```

## 3. Activate the Environment

- **Windows (Command Prompt):**
  ```
  env_selfhost\Scripts\activate.bat
  ```
- **Windows (PowerShell):**
  ```
  .\env_selfhost\Scripts\Activate.ps1
  ```
- **Linux/macOS:**
  ```
  source env_selfhost/bin/activate
  ```

## 4. Install Python Packages

After activating the environment, install dependencies:
```
pip install -r requirements.txt
```

## 5. Start the FastAPI Application

Run the following command:
```
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

## 6. Using Models

- **Enterprise Models:**  
  To use enterprise models (e.g., GCP, Azure, Bedrock), add them to `enterprise_models.py` as shown:
  ```python
  models = {
      "bedrock": [
          { "value": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "label": "Bedrock (Claude 3 Haiku)" }
      ],
      "azure": [
          { "value": "azure/openai.gpt-4", "label": "Azure (GPT-4)" }
      ],
      "gcp": [
          { "value": "gcp/google.gemini-pro", "label": "GCP (Gemini Pro)" }
      ]
  }
  ```
- **Ollama Models:**  
  Download and install Ollama from [https://ollama.com/download](https://ollama.com/download) to use local models.

## 7. API Documentation

Once running, access [http://localhost:8080/docs](http://localhost:8080/docs) for interactive Swagger documentation.

# Running on AWS ECS

This project is designed for secure, scalable, and cost-efficient LLM orchestration on AWS ECS.  
For full infrastructure setup, see the [Medium blog post](https://medium.com/@anveshshada/decoupling-master-application-from-gpu-bound-llm-tasks-on-ecs-with-ollama-57584ff25ada).

## Architecture Overview

- **Master Application**: FastAPI app on ECS Fargate (orchestrates requests, manages GPU cluster lifecycle).
- **Executor Cluster**: ECS EC2 GPU instances running Ollama containers for inference.
- **Networking**: Dedicated VPC, public/private subnets, NAT instance, strict security groups.
- **Service Connect**: Secure internal DNS-based service discovery between master and executor.

## Prerequisites

- AWS account with permissions for ECS, EC2, VPC, IAM, CloudWatch, and ECR.
- Docker installed locally for building images.
- [Blog post](https://medium.com/@anveshshada/decoupling-master-application-from-gpu-bound-llm-tasks-on-ecs-with-ollama-57584ff25ada) covers VPC, subnet, NAT, and security group setup.

## Steps to Deploy

### 1. Build & Push Docker Images

- **Master App**:
  ```sh
  docker build -t master-app -f Dockerfile_master_app .
  # Tag and push to ECR
  ```
- **Ollama Executor**:
  ```sh
  docker build -t ollama-executor -f Dockerfile_ollama .
  # Tag and push to ECR
  ```

### 2. Provision AWS Infrastructure

- Create VPC (`10.0.0.0/16`), public/private subnets, NAT instance, and security groups as described in the blog.
- Set up ECS clusters:
  - **Master**: Fargate service in private subnet, behind ALB.
  - **Executor**: EC2 GPU-backed ECS service in private subnet.

### 3. Configure ECS Task Definitions

#### Master App Task Definition (Redacted Example)
```json
{
  "family": "master-app-task-def",
  "containerDefinitions": [
    {
      "name": "fastapi-application",
      "image": "<your_ecr_repo>/master-app:latest",
      "portMappings": [
        { "containerPort": 8080, "hostPort": 8080 }
      ],
      "essential": true,
      "environment": [
        { "name": "SERVICE_NAME", "value": "<executor_service_name>" },
        { "name": "SUBNET_ID", "value": "<subnet_id>" },
        { "name": "AUTOSCALING_GROUP_NAME", "value": "<asg_name>" },
        { "name": "CLUSTER_NAME", "value": "<ecs_cluster_name>" },
        { "name": "OLLAMA_HOST", "value": "http://selfhostllm:11434" },
        { "name": "ROUTE_TABLE_ID", "value": "<route_table_id>" },
        { "name": "ALLOCATION_ID", "value": "<eip_allocation_id>" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/master-app-task-def",
          "awslogs-region": "<region>",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ],
  "cpu": "1024",
  "memory": "3072",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "runtimePlatform": {
    "cpuArchitecture": "X86_64",
    "operatingSystemFamily": "LINUX"
  }
}
```

#### Executor Task Definition (Redacted Example)
```json
{
  "family": "ollama-executor-task-def",
  "containerDefinitions": [
    {
      "name": "ollama-server",
      "image": "<your_ecr_repo>/ollama:latest",
      "portMappings": [
        { "containerPort": 11434, "hostPort": 11434 }
      ],
      "essential": true,
      "memory": 12288,
      "cpu": 3072,
      "resourceRequirements": [
        { "type": "GPU", "value": "1" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/ollama-executor-task-def",
          "awslogs-region": "<region>",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ],
  "cpu": "3072",
  "memory": "12288",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["EC2"],
  "runtimePlatform": {
    "cpuArchitecture": "X86_64",
    "operatingSystemFamily": "LINUX"
  }
}
```

### 4. Deploy Services

- Use ECS console or IaC (CloudFormation/Terraform) to launch services.
- Ensure ALB routes traffic to master app.
- Executors only accept traffic from master via security group rules.

### 5. Model Management & Inference

- Use FastAPI endpoints for orchestration:
  - `/start_selfhost_llm` and `/shutdown_selfhost_llm` to scale GPU executors.
  - `/pull_model/{model_name}` to download models (NAT provisioned as needed).
  - `/generate` for streaming inference.
  - `/list_models` for available models.
- See [API docs](http://<your-alb-dns>:8080/docs) after deployment.

### 6. Security & Networking

- All traffic is routed internally via Service Connect and security groups.
- NAT instance enables outbound traffic for model downloads only when needed.
- ALB restricts access to trusted IPs.

# Testing with Web UI

You can interact with your FastAPI orchestration endpoints using the included `ui_latest.html` file. This web UI works for both local development and AWS deployments.

## Localhost Usage

1. **Start FastAPI Locally**  
   Run the FastAPI app on your machine (see instructions above).

2. **Open the Web UI**  
   - Locate `ui_latest.html` in your project directory.
   - Double-click to open it in your browser.

3. **Connect to API**  
   - Enter `localhost` and your FastAPI port (e.g., `8080`) in the UI.
   - Click **"Test Connection"** to verify.

4. **Use Features**  
   - Manage models (pull, delete, list).
   - Control local GPU executors (if configured).
   - Chat with LLMs and export chat history.

## AWS Usage

1. **Deploy FastAPI & Executors on AWS ECS**  
   Ensure your services are running and accessible via your ALB DNS.

2. **Open the Web UI**  
   - Use the same `ui_latest.html` file locally.

3. **Connect to API**  
   - Enter your ALB DNS name and port (e.g., `my-alb-dns.amazonaws.com:8080`) in the UI.
   - Click **"Test Connection"**.

4. **Use Features**  
   - Manage models and orchestrate GPU executors on AWS.
   - Chat with LLMs deployed on ECS.
   - Export chat history.

---

**Note:**  
No additional server is required for the UI. It runs in your browser and communicates directly with the FastAPI backend (local or AWS) via HTTP.
