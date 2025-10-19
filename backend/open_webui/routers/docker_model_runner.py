# Docker Model Runner - Ollama-compatible API for bundled model inference
# This provides equal functionality to Ollama but designed to run within the Docker container

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime

from typing import Optional, Union
from urllib.parse import urlparse
import aiohttp
from aiocache import cached
import requests
from urllib.parse import quote

from open_webui.models.chats import Chats
from open_webui.models.users import UserModel

from open_webui.env import (
    ENABLE_FORWARD_USER_INFO_HEADERS,
)

from fastapi import (
    Depends,
    HTTPException,
    Request,
    APIRouter,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, validator
from starlette.background import BackgroundTask

from open_webui.models.models import Models
from open_webui.utils.payload import (
    apply_model_params_to_body_ollama,
    apply_system_prompt_to_body,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access

from open_webui.env import (
    ENV,
    SRC_LOG_LEVELS,
    MODELS_CACHE_TTL,
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    BYPASS_MODEL_ACCESS_CONTROL,
)
from open_webui.constants import ERROR_MESSAGES

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("DOCKER_MODEL_RUNNER", SRC_LOG_LEVELS.get("OLLAMA")))

router = APIRouter()


# Utility functions (similar to Ollama router)
async def send_get_request(url, key=None, user: UserModel = None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **(
                        {
                            "X-OpenWebUI-User-Name": quote(user.name, safe=" "),
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user
                        else {}
                    ),
                },
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                return await response.json()
    except Exception as e:
        log.error(f"Connection error: {e}")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


async def send_post_request(
    url: str,
    payload: Union[str, bytes],
    stream: bool = True,
    key: Optional[str] = None,
    content_type: Optional[str] = None,
    user: UserModel = None,
    metadata: Optional[dict] = None,
):
    r = None
    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {key}"} if key else {}),
                **(
                    {
                        "X-OpenWebUI-User-Name": quote(user.name, safe=" "),
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                        **(
                            {"X-OpenWebUI-Chat-Id": metadata.get("chat_id")}
                            if metadata and metadata.get("chat_id")
                            else {}
                        ),
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS and user
                    else {}
                ),
            },
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )

        if r.ok is False:
            try:
                res = await r.json()
                await cleanup_response(r, session)
                if "error" in res:
                    raise HTTPException(status_code=r.status, detail=res["error"])
            except HTTPException as e:
                raise e
            except Exception as e:
                log.error(f"Failed to parse error response: {e}")
                raise HTTPException(
                    status_code=r.status,
                    detail=f"Open WebUI: Server Connection Error",
                )

        r.raise_for_status()
        if stream:
            response_headers = dict(r.headers)
            if content_type:
                response_headers["Content-Type"] = content_type

            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=response_headers,
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            res = await r.json()
            return res

    except HTTPException as e:
        raise e
    except Exception as e:
        detail = f"Docker Model Runner: {e}"
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if e else "Open WebUI: Server Connection Error",
        )
    finally:
        if not stream:
            await cleanup_response(r, session)


def get_api_key(idx, url, configs):
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return configs.get(str(idx), configs.get(base_url, {})).get("key", None)


@router.head("/")
@router.get("/")
async def get_status():
    return {"status": True}


class ConnectionVerificationForm(BaseModel):
    url: str
    key: Optional[str] = None


@router.post("/verify")
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
    url = form_data.url
    key = form_data.key

    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST),
    ) as session:
        try:
            async with session.get(
                f"{url}/api/version",
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **(
                        {
                            "X-OpenWebUI-User-Name": quote(user.name, safe=" "),
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user
                        else {}
                    ),
                },
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as r:
                if r.status != 200:
                    detail = f"HTTP Error: {r.status}"
                    res = await r.json()
                    if "error" in res:
                        detail = f"External Error: {res['error']}"
                    raise Exception(detail)

                data = await r.json()
                return data
        except aiohttp.ClientError as e:
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(
                status_code=500, detail="Open WebUI: Server Connection Error"
            )
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            error_detail = f"Unexpected error: {str(e)}"
            raise HTTPException(status_code=500, detail=error_detail)


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_DOCKER_MODEL_RUNNER_API": request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API,
        "DOCKER_MODEL_RUNNER_BASE_URLS": request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS,
        "DOCKER_MODEL_RUNNER_API_CONFIGS": request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS,
    }


class DockerModelRunnerConfigForm(BaseModel):
    ENABLE_DOCKER_MODEL_RUNNER_API: Optional[bool] = None
    DOCKER_MODEL_RUNNER_BASE_URLS: list[str]
    DOCKER_MODEL_RUNNER_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(
    request: Request, form_data: DockerModelRunnerConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API = form_data.ENABLE_DOCKER_MODEL_RUNNER_API
    request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS = form_data.DOCKER_MODEL_RUNNER_BASE_URLS
    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS = form_data.DOCKER_MODEL_RUNNER_API_CONFIGS

    keys = list(map(str, range(len(request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS))))
    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS = {
        key: value
        for key, value in request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.items()
        if key in keys
    }

    return {
        "ENABLE_DOCKER_MODEL_RUNNER_API": request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API,
        "DOCKER_MODEL_RUNNER_BASE_URLS": request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS,
        "DOCKER_MODEL_RUNNER_API_CONFIGS": request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS,
    }


def merge_models_lists(model_lists):
    merged_models = {}
    for idx, model_list in enumerate(model_lists):
        if model_list is not None:
            for model in model_list:
                id = model.get("model")
                if id is not None:
                    if id not in merged_models:
                        model["urls"] = [idx]
                        merged_models[id] = model
                    else:
                        merged_models[id]["urls"].append(idx)
    return list(merged_models.values())


@cached(
    ttl=MODELS_CACHE_TTL,
    key=lambda _, user: f"docker_model_runner_all_models_{user.id}" if user else "docker_model_runner_all_models",
)
async def get_all_models(request: Request, user: UserModel = None):
    log.info("Docker Model Runner: get_all_models()")
    if request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API:
        request_tasks = []
        for idx, url in enumerate(request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS):
            if (str(idx) not in request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS) and (
                url not in request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS
            ):
                request_tasks.append(send_get_request(f"{url}/api/tags", user=user))
            else:
                api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
                    str(idx),
                    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
                )

                enable = api_config.get("enable", True)
                key = api_config.get("key", None)

                if enable:
                    request_tasks.append(
                        send_get_request(f"{url}/api/tags", key, user=user)
                    )
                else:
                    request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

        responses = await asyncio.gather(*request_tasks)

        for idx, response in enumerate(responses):
            if response:
                url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[idx]
                api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
                    str(idx),
                    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
                )

                connection_type = api_config.get("connection_type", "docker")
                prefix_id = api_config.get("prefix_id", None)
                tags = api_config.get("tags", [])
                model_ids = api_config.get("model_ids", [])

                if len(model_ids) != 0 and "models" in response:
                    response["models"] = list(
                        filter(
                            lambda model: model["model"] in model_ids,
                            response["models"],
                        )
                    )

                for model in response.get("models", []):
                    if prefix_id:
                        model["model"] = f"{prefix_id}.{model['model']}"
                    if tags:
                        model["tags"] = tags
                    if connection_type:
                        model["connection_type"] = connection_type

        models = {
            "models": merge_models_lists(
                map(
                    lambda response: response.get("models", []) if response else None,
                    responses,
                )
            )
        }

        try:
            loaded_models = await get_loaded_models(request, user=user)
            expires_map = {
                m["model"]: m["expires_at"]
                for m in loaded_models["models"]
                if "expires_at" in m
            }

            for m in models["models"]:
                if m["model"] in expires_map:
                    dt = datetime.fromisoformat(expires_map[m["model"]])
                    m["expires_at"] = int(dt.timestamp())
        except Exception as e:
            log.debug(f"Failed to get loaded models: {e}")

    else:
        models = {"models": []}

    request.app.state.DOCKER_MODEL_RUNNER_MODELS = {
        model["model"]: model for model in models["models"]
    }
    return models


async def get_filtered_models(models, user):
    filtered_models = []
    for model in models.get("models", []):
        model_info = Models.get_model_by_id(model["model"])
        if model_info:
            if user.id == model_info.user_id or has_access(
                user.id, type="read", access_control=model_info.access_control
            ):
                filtered_models.append(model)
    return filtered_models


@router.get("/api/tags")
@router.get("/api/tags/{url_idx}")
async def get_tags(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    models = []

    if url_idx is None:
        models = await get_all_models(request, user=user)
    else:
        url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[url_idx]
        key = get_api_key(url_idx, url, request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS)

        r = None
        try:
            r = requests.request(
                method="GET",
                url=f"{url}/api/tags",
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **(
                        {
                            "X-OpenWebUI-User-Name": quote(user.name, safe=" "),
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user
                        else {}
                    ),
                },
            )
            r.raise_for_status()
            models = r.json()
        except Exception as e:
            log.exception(e)
            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"Docker Model Runner: {res['error']}"
                except Exception:
                    detail = f"Docker Model Runner: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500,
                detail=detail if detail else "Open WebUI: Server Connection Error",
            )

    if user.role == "user" and not BYPASS_MODEL_ACCESS_CONTROL:
        models["models"] = await get_filtered_models(models, user)

    return models


@router.get("/api/ps")
async def get_loaded_models(request: Request, user=Depends(get_admin_user)):
    """List models currently loaded into Docker Model Runner memory."""
    if request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API:
        request_tasks = []
        for idx, url in enumerate(request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS):
            if (str(idx) not in request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS) and (
                url not in request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS
            ):
                request_tasks.append(send_get_request(f"{url}/api/ps", user=user))
            else:
                api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
                    str(idx),
                    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
                )

                enable = api_config.get("enable", True)
                key = api_config.get("key", None)

                if enable:
                    request_tasks.append(
                        send_get_request(f"{url}/api/ps", key, user=user)
                    )
                else:
                    request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

        responses = await asyncio.gather(*request_tasks)

        for idx, response in enumerate(responses):
            if response:
                url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[idx]
                api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
                    str(idx),
                    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
                )

                prefix_id = api_config.get("prefix_id", None)

                for model in response.get("models", []):
                    if prefix_id:
                        model["model"] = f"{prefix_id}.{model['model']}"

        models = {
            "models": merge_models_lists(
                map(
                    lambda response: response.get("models", []) if response else None,
                    responses,
                )
            )
        }
    else:
        models = {"models": []}

    return models


@router.get("/api/version")
@router.get("/api/version/{url_idx}")
async def get_versions(request: Request, url_idx: Optional[int] = None):
    if request.app.state.config.ENABLE_DOCKER_MODEL_RUNNER_API:
        if url_idx is None:
            request_tasks = []

            for idx, url in enumerate(request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS):
                api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
                    str(idx),
                    request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
                )

                enable = api_config.get("enable", True)
                key = api_config.get("key", None)

                if enable:
                    request_tasks.append(
                        send_get_request(f"{url}/api/version", key)
                    )

            responses = await asyncio.gather(*request_tasks)
            responses = list(filter(lambda x: x is not None, responses))

            if len(responses) > 0:
                lowest_version = min(
                    responses,
                    key=lambda x: tuple(
                        map(int, re.sub(r"^v|-.*", "", x["version"]).split("."))
                    ),
                )
                return {"version": lowest_version["version"]}
            else:
                raise HTTPException(
                    status_code=500,
                    detail="Docker Model Runner not found",
                )
        else:
            url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[url_idx]
            r = None
            try:
                r = requests.request(method="GET", url=f"{url}/api/version")
                r.raise_for_status()
                return r.json()
            except Exception as e:
                log.exception(e)
                detail = None
                if r is not None:
                    try:
                        res = r.json()
                        if "error" in res:
                            detail = f"Docker Model Runner: {res['error']}"
                    except Exception:
                        detail = f"Docker Model Runner: {e}"

                raise HTTPException(
                    status_code=r.status_code if r else 500,
                    detail=detail if detail else "Open WebUI: Server Connection Error",
                )
    else:
        return {"version": False}


class ModelNameForm(BaseModel):
    model: Optional[str] = None
    model_config = ConfigDict(extra="allow")


@router.post("/api/pull")
@router.post("/api/pull/{url_idx}")
async def pull_model(
    request: Request,
    form_data: ModelNameForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    form_data = form_data.model_dump(exclude_none=True)
    form_data["model"] = form_data.get("model", form_data.get("name"))

    url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[url_idx]
    log.info(f"url: {url}")

    payload = {**form_data, "insecure": True}

    return await send_post_request(
        url=f"{url}/api/pull",
        payload=json.dumps(payload),
        key=get_api_key(url_idx, url, request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS),
        user=user,
    )


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    images: Optional[list[str]] = None

    @validator("content", pre=True)
    @classmethod
    def check_at_least_one_field(cls, field_value, values, **kwargs):
        if field_value is None and (
            "tool_calls" not in values or values["tool_calls"] is None
        ):
            raise ValueError(
                "At least one of 'content' or 'tool_calls' must be provided"
            )
        return field_value


class GenerateChatCompletionForm(BaseModel):
    model: str
    messages: list[ChatMessage]
    format: Optional[Union[dict, str]] = None
    options: Optional[dict] = None
    template: Optional[str] = None
    stream: Optional[bool] = True
    keep_alive: Optional[Union[int, str]] = None
    tools: Optional[list[dict]] = None
    model_config = ConfigDict(extra="allow")


async def get_runner_url(request: Request, model: str, url_idx: Optional[int] = None):
    if url_idx is None:
        models = request.app.state.DOCKER_MODEL_RUNNER_MODELS
        if model not in models:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.MODEL_NOT_FOUND(model),
            )
        url_idx = random.choice(models[model].get("urls", []))
    url = request.app.state.config.DOCKER_MODEL_RUNNER_BASE_URLS[url_idx]
    return url, url_idx


@router.post("/api/chat")
@router.post("/api/chat/{url_idx}")
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    url_idx: Optional[int] = None,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    metadata = form_data.pop("metadata", None)
    try:
        form_data = GenerateChatCompletionForm(**form_data)
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=400, detail=str(e))

    if isinstance(form_data, BaseModel):
        payload = {**form_data.model_dump(exclude_none=True)}

    if "metadata" in payload:
        del payload["metadata"]

    model_id = payload["model"]
    model_info = Models.get_model_by_id(model_id)

    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id

        params = model_info.params.model_dump()

        if params:
            system = params.pop("system", None)
            payload = apply_model_params_to_body_ollama(params, payload)
            payload = apply_system_prompt_to_body(system, payload, metadata, user)

        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
            ):
                raise HTTPException(status_code=403, detail="Model not found")
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Model not found")

    if ":" not in payload["model"]:
        payload["model"] = f"{payload['model']}:latest"

    url, url_idx = await get_runner_url(request, payload["model"], url_idx)
    api_config = request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(
        str(url_idx),
        request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS.get(url, {}),
    )

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    return await send_post_request(
        url=f"{url}/api/chat",
        payload=json.dumps(payload),
        stream=form_data.stream,
        key=get_api_key(url_idx, url, request.app.state.config.DOCKER_MODEL_RUNNER_API_CONFIGS),
        content_type="application/x-ndjson",
        user=user,
        metadata=metadata,
    )
