from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from litellm import acompletion  # ✅ async version of completion
import os
import httpx
from fastapi import HTTPException
import json

import boto3
import time
import asyncio

from enterprise_models import models  # Import your enterprise models

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

logger= logging.getLogger(__name__)

app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or restrict to your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# get ollama host
OLLAMA_HOST= os.getenv("OLLAMA_HOST", "http://localhost:11434")

logger.info(f"Using OLLAMA_HOST: {OLLAMA_HOST}")


@app.get("/healthz")
def health():
    logger.info("Health check endpoint called")
    return {"status": "ok"}


# --- AWS NAT Gateway Helpers ---
SUBNET_ID = os.environ.get("SUBNET_ID")
ALLOCATION_ID = os.environ.get("ALLOCATION_ID")
ROUTE_TABLE_ID = os.environ.get("ROUTE_TABLE_ID")

def get_ec2_client():
    return boto3.client('ec2')

def check_nat_gateway_status(ec2_client):
    """Check if a NAT Gateway is available in the subnet."""
    response = ec2_client.describe_nat_gateways(
        Filters=[
            {"Name": "subnet-id", "Values": [SUBNET_ID]},
            {"Name": "state", "Values": ["available"]}
        ]
    )
    return response['NatGateways'][0]['NatGatewayId'] if response['NatGateways'] else None

def create_nat_gateway(ec2_client):
    """Create a NAT Gateway in the subnet."""
    try:
        response = ec2_client.create_nat_gateway(
            SubnetId=SUBNET_ID,
            ConnectivityType='public',
            AllocationId=ALLOCATION_ID,
            TagSpecifications=[{
                'ResourceType': 'natgateway',
                'Tags': [
                    {'Key': 'Name', 'Value': 'executor-nat-gateway'},
                    {'Key': 'Environment', 'Value': 'Production'},
                    {'Key': 'Owner', 'Value': 'Anvesh'}
                ]
            }]
        )
        nat_id = response['NatGateway']['NatGatewayId']
        logger.info(f"Created NAT Gateway: {nat_id}")
        return nat_id
    except Exception as e:
        logger.error(f"Failed to create NAT Gateway: {e}")
        return None

def is_nat_gateway_available(ec2_client, nat_gateway_id):
    """Check if a specific NAT Gateway is available."""
    try:
        response = ec2_client.describe_nat_gateways(
            Filters=[
                {"Name": "nat-gateway-id", "Values": [nat_gateway_id]},
                {"Name": "state", "Values": ["available"]}
            ]
        )
        return bool(response['NatGateways'])
    except Exception as e:
        logger.error(f"Error checking NAT Gateway status: {e}")
        return False

def attach_nat_gateway_to_route_table(ec2_client, nat_gateway_id):
    """Attach the NAT Gateway to the route table."""
    try:
        ec2_client.replace_route(
            RouteTableId=ROUTE_TABLE_ID,
            DestinationCidrBlock='0.0.0.0/0',
            NatGatewayId=nat_gateway_id
        )
        logger.info(f"Attached NAT Gateway {nat_gateway_id} to Route Table {ROUTE_TABLE_ID}")
        return True
    except Exception as e:
        logger.error(f"Failed to attach NAT Gateway to Route Table: {e}")
        return False

def delete_nat_gateway(ec2_client, nat_gateway_id):
    """Delete the specified NAT Gateway."""
    try:
        ec2_client.delete_nat_gateway(NatGatewayId=nat_gateway_id)
        logger.info(f"Deleted NAT Gateway: {nat_gateway_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete NAT Gateway: {e}")
        return False

def get_current_default_route(ec2_client):
    """Get the current default route for the route table."""
    try:
        response = ec2_client.describe_route_tables(RouteTableIds=[ROUTE_TABLE_ID])
        routes = response['RouteTables'][0]['Routes']
        for route in routes:
            if route.get('DestinationCidrBlock') == '0.0.0.0/0':
                # Could be NatGatewayId, GatewayId (IGW), etc.
                return {
                    "NatGatewayId": route.get("NatGatewayId"),
                    "GatewayId": route.get("GatewayId"),
                    "InstanceId": route.get("InstanceId"),
                    "NetworkInterfaceId": route.get("NetworkInterfaceId"),
                }
        return None
    except Exception as e:
        logger.error(f"Failed to get current default route: {e}")
        return None

def restore_default_route(ec2_client, original_route):
    """Restore the default route to its original target."""
    try:
        kwargs = {
            "RouteTableId": ROUTE_TABLE_ID,
            "DestinationCidrBlock": "0.0.0.0/0"
        }
        # Remove all possible targets, add only the original one
        for key in ["NatGatewayId", "GatewayId", "InstanceId", "NetworkInterfaceId"]:
            if original_route.get(key):
                kwargs[key] = original_route[key]
                break  # Only one target should be set

        ec2_client.replace_route(**kwargs)
        logger.info(f"Restored default route to original target: {original_route}")
        return True
    except Exception as e:
        logger.error(f"Failed to restore default route: {e}")
        return False

# --- Model Pull Task ---

async def pull_model_task(model_name: str):
    if os.getenv("OLLAMA_HOST",""): # If OLLAMA_HOST is set, we assume Ollama is running on the host
        ec2_client = get_ec2_client()
        nat_gateway_id = check_nat_gateway_status(ec2_client)
        original_route = get_current_default_route(ec2_client)  # <-- Save original route

        if nat_gateway_id:
            logger.info(f"NAT Gateway already available: {nat_gateway_id}")
        else:
            nat_gateway_id = create_nat_gateway(ec2_client)
            if not nat_gateway_id:
                logger.error("Failed to create NAT Gateway, aborting model pull.")
                return

            logger.info("Waiting for NAT Gateway to become available...")
            for _ in range(40):  # up to ~10 minutes
                if is_nat_gateway_available(ec2_client, nat_gateway_id):
                    logger.info("NAT Gateway is now available.")
                    break
                time.sleep(15)
            else:
                logger.error("NAT Gateway did not become available in time.")
                return

        attach_nat_gateway_to_route_table(ec2_client, nat_gateway_id)
        time.sleep(20)  # Ensure route is updated

    # Pull model from Ollama
    async with httpx.AsyncClient(timeout=None) as client:
        for attempt in range(10):
            try:
                logger.info(f"Pulling model attempt {attempt+1} for {model_name}")
                resp = await client.post(f"{OLLAMA_HOST}/api/pull", json={"name": model_name})
                logger.info(f"Pull model response status: {resp.status_code}, body: {resp.text}")
            except Exception as e:
                logger.error(f"Exception during model pull: {e}")

            await asyncio.sleep(60)
            try:
                resp_tags = await client.get(f"{OLLAMA_HOST}/api/tags")
                if resp_tags.status_code == 200:
                    models = resp_tags.json().get("models", [])
                    if any(m.get("name") == model_name for m in models):
                        logger.info(f"Model {model_name} is now available locally.")
                        break
            except Exception as e:
                logger.error(f"Exception during model tag check: {e}")
        else:
            logger.warning(f"Model {model_name} was not available after 10 attempts.")

    # Clean up NAT Gateway if created
    if os.getenv("OLLAMA_HOST",""):  # Only clean up if we hosted Ollama on the host
        if nat_gateway_id:
            delete_nat_gateway(ec2_client, nat_gateway_id)
            logger.info(f"NAT Gateway {nat_gateway_id} deleted after model pull.")
            # Restore the original route
            if original_route:
                restore_default_route(ec2_client, original_route)

def run_async_task(model_name: str):
    asyncio.run(pull_model_task(model_name))

@app.post("/pull_model/{model_name}")
async def pull_model(model_name: str, background_tasks: BackgroundTasks):
    """Trigger a model pull from Ollama in the background, managing NAT Gateway as needed."""
    logger.info(f"Received request to pull model: {model_name}")
    background_tasks.add_task(run_async_task, model_name)
    return {"status": "pull_started", "model": model_name}

@app.get("/check_model/{model_name}")
async def check_model(model_name: str):
    """Check if a model is available locally"""
    logger.info(f"Checking availability for model: {model_name}")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{OLLAMA_HOST}/api/tags")
        logger.info(f"Check model response status: {resp.status_code}, body: {resp.text}")
        if resp.status_code != 200:
            logger.error(f"Failed to check model {model_name}: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        
        models = resp.json().get("models", [])
        for m in models:
            if m.get("name") == model_name:
                logger.info(f"Model {model_name} is available locally.")
                return {"available": True, "model": m}
        
        logger.info(f"Model {model_name} is NOT available locally.")
        return {"available": False}

@app.delete("/delete_model/{model_name}")
async def delete_model(model_name: str):
    """Delete a local Ollama model"""
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            "DELETE",
            f"{OLLAMA_HOST}/api/delete",
            content=json.dumps({"name": model_name}),
            headers={"Content-Type": "application/json"}
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return {"status": "deleted", "model": model_name}

@app.post("/generate")
async def generate_stream(request_data: dict):
    """Generate streaming response using Server-Sent Events (SSE)"""
    
    async def event_stream():
        try:
            logger.info(f"Received data for streaming generation: {request_data}")

            # Accept messages array from client
            messages = request_data.get("messages")
            model_name = request_data.get("model")

            if not messages or not isinstance(messages, list):
                yield f"data: Error: 'messages' must be a list of {{'role', 'content'}} dicts.\n\n"
                return

            parameters = {
                "model": model_name,
                "messages": messages,
                "stream": True
            }

            if model_name.startswith("bedrock/"):
                parameters["temperature"] = 0.7
                parameters["aws_region_name"] = "us-east-1"
            elif model_name.startswith("ollama/"):
                parameters["api_base"] = OLLAMA_HOST

            logger.info(f"Requesting completion for model: {model_name} with parameters: {parameters}")
            response = await acompletion(**parameters)

            async for chunk in response:
                content = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if not content:
                    content = chunk.get("completion")
                if content:
                    logger.debug(f"Sending chunk via SSE: {content}")
                    # Escape newlines and special characters for SSE format
                    escaped_content = content.replace('\n', '\\n').replace('\r', '\\r')
                    yield f"data: {escaped_content}\n\n"

            logger.info("Sending [DONE] event to SSE client")
            yield f"data: [DONE]\n\n"

        except Exception as e:
            import traceback
            logger.exception(f"Exception in generate_stream: {str(e)}")
            print("Exception:", traceback.format_exc())
            yield f"data: Error: {str(e)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )

def list_ecs_tasks(cluster_name: str, service_name: str):
    """List all tasks in the specified ECS service."""
    ecs = boto3.client("ecs")
    try:
        response = ecs.list_tasks(
            cluster=cluster_name,
            serviceName=service_name
        )
        return response['taskArns']
    except Exception as e:
        logger.error(f"Failed to list ECS tasks: {e}")
        return []

def stop_ecs_tasks(cluster_name: str, service_name: str):
    """List tasks in the ECS service and stop them."""
    tasks = list_ecs_tasks(cluster_name, service_name)
    if not tasks:
        logger.info(f"No tasks found in service {service_name}.")
        return

    ecs = boto3.client("ecs")
    for task in tasks:
        try:
            ecs.stop_task(
                cluster=cluster_name,
                task=task
            )
            logger.info(f"Stopped task {task} in service {service_name}.")
        except Exception as e:
            logger.error(f"Failed to stop task {task}: {e}")

@app.get("/shutdown_selfhost_llm")
def shutdown_ecs_service():
    """
    Set ECS service desired count and ASG desired capacity to 0.
    Also stop all ECS tasks associated with the service.
    """
    cluster_name = os.environ.get("CLUSTER_NAME")
    service_name = os.environ.get("SERVICE_NAME")
    autoscaling_group_name = os.environ.get("AUTOSCALING_GROUP_NAME")
    if not all([cluster_name, service_name, autoscaling_group_name]):
        raise HTTPException(status_code=400, detail="Missing environment variables for cluster/service/asg name.")

    ecs = boto3.client("ecs")
    asg = boto3.client("autoscaling")
    try:
        asg.update_auto_scaling_group(
            AutoScalingGroupName=autoscaling_group_name,
            DesiredCapacity=0,
            MinSize=0
        )
        logger.info(f"Set ASG {autoscaling_group_name} desired capacity to 0.")
        
        ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            desiredCount=0,
            forceNewDeployment=True
        )
        logger.info(f"Set ECS service {service_name} desired count to 0.")

        # Stop all ECS tasks in the service
        stop_ecs_tasks(cluster_name, service_name)

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error shutting down ECS/ASG: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/start_selfhost_llm")
def start_ecs_service():
    """
    Set ECS service desired count and ASG desired capacity to 1.
    """
    cluster_name = os.environ.get("CLUSTER_NAME")
    service_name = os.environ.get("SERVICE_NAME")
    autoscaling_group_name = os.environ.get("AUTOSCALING_GROUP_NAME")
    if not all([cluster_name, service_name, autoscaling_group_name]):
        raise HTTPException(status_code=400, detail="Missing environment variables for cluster/service/asg name.")

    ecs = boto3.client("ecs")
    asg = boto3.client("autoscaling")
    try:
        asg.update_auto_scaling_group(
            AutoScalingGroupName=autoscaling_group_name,
            DesiredCapacity=1,
            MinSize=0
        )
        logger.info(f"Set ASG {autoscaling_group_name} desired capacity to 1.")
        
        ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            desiredCount=1,
            forceNewDeployment=True
        )
        logger.info(f"Set ECS service {service_name} desired count to 1.")


        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error starting ECS/ASG: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def get_ecs_task_status(cluster_name: str, task_arn: str):
    """Get the status of a specific ECS task."""
    ecs = boto3.client("ecs")
    try:
        response = ecs.describe_tasks(
            cluster=cluster_name,
            tasks=[task_arn]
        )
        return response['tasks'][0]['lastStatus']
    except Exception as e:
        logger.error(f"Failed to get ECS task status: {e}")
        return None

@app.get("/selfhost_status")
def selfhost_status():
    """
    Check if the self-hosted LLM ECS service is ready.
    Returns the status of the latest ECS task in the service.
    """
    cluster_name = os.environ.get("CLUSTER_NAME")
    service_name = os.environ.get("SERVICE_NAME")
    if not all([cluster_name, service_name]):
        raise HTTPException(status_code=400, detail="Missing environment variables for cluster/service name.")

    # Get all tasks for the service
    tasks = list_ecs_tasks(cluster_name, service_name)
    if not tasks:
        return {"ready": False, "status": "NO_TASKS"}

    # Get the status of the most recent task (last in list)
    latest_task_arn = tasks[-1]
    status = get_ecs_task_status(cluster_name, latest_task_arn)
    ready = status == "RUNNING"
    return {"ready": ready, "status": status, "task_arn": latest_task_arn}

# @app.get("/list_models")
# async def list_models():
#     """List all available models from Ollama and enterprise models."""
#     logger.info("Listing all available models from Ollama and enterprise models.")
#     enterprise_models = sum(models.values(),[])
#     async with httpx.AsyncClient() as client:
#         resp = await client.get(f"{OLLAMA_HOST}/api/tags")
#         if resp.status_code != 200:
#             raise HTTPException(status_code=resp.status_code, detail=resp.text)
#         ollama_models_response = resp.json().get("models", [])
    
#     ollma_models= [{"value": f"ollama/{m['name']}", "label": f"ollama ({m['name']})"} for m in ollama_models_response]
#     all_models = ollma_models + enterprise_models
#     return {"models": all_models}

@app.get("/list_models")
async def list_models():
    """List all available models from Ollama and enterprise models."""
    logger.info("Listing all available models from Ollama and enterprise models.")
    
    # Always include enterprise models
    enterprise_models = sum(models.values(), [])
    ollama_models = []

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
            resp.raise_for_status()  # raises for 4xx/5xx

            ollama_models_response = resp.json().get("models", [])
            ollama_models = [
                {"value": f"ollama/{m['name']}", "label": f"ollama ({m['name']})"}
                for m in ollama_models_response
            ]
    except Exception as e:
        # Log error but don’t break
        logger.warning(f"Failed to fetch Ollama models: {e}")

    all_models = ollama_models + enterprise_models
    return {"models": all_models}
