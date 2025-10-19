# Docker Model Runner

Docker Model Runner is an LLM provider integration for Open WebUI that provides Ollama-compatible API functionality with equal or better features than Ollama.

## Features

- **Ollama-Compatible API**: Fully compatible with Ollama API endpoints
- **Bundled Docker Support**: Can be included in the Open WebUI Docker container
- **Multiple Instance Support**: Configure multiple Docker Model Runner instances
- **Load Balancing**: Round-robin distribution across multiple backends
- **Model Management**: Pull, push, create, copy, and delete models
- **Chat Completions**: Stream and non-stream chat completions
- **Embeddings**: Generate embeddings for RAG and search
- **Access Control**: Per-model access control and user permissions
- **Model Prefixing**: Add prefixes to differentiate models from different sources

## Installation

### Using Docker Build Args

Build Open WebUI with Docker Model Runner bundled:

```bash
docker build \
  --build-arg USE_DOCKER_MODEL_RUNNER=true \
  -t open-webui-with-docker-model-runner .
```

### Using Docker Compose

Add to your `docker-compose.yaml`:

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    build:
      args:
        - USE_DOCKER_MODEL_RUNNER=true
    environment:
      - ENABLE_DOCKER_MODEL_RUNNER_API=true
      - DOCKER_MODEL_RUNNER_BASE_URL=http://localhost:11435
    ports:
      - "3000:8080"
    volumes:
      - open-webui:/app/backend/data
```

### Standalone Docker Model Runner

Run Docker Model Runner separately:

```bash
# Start Docker Model Runner on custom port
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

Then configure Open WebUI to connect:

```bash
docker run -d \
  -p 3000:8080 \
  -e ENABLE_DOCKER_MODEL_RUNNER_API=true \
  -e DOCKER_MODEL_RUNNER_BASE_URLS="http://host.docker.internal:11435" \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_DOCKER_MODEL_RUNNER_API` | `true` | Enable/disable Docker Model Runner integration |
| `DOCKER_MODEL_RUNNER_BASE_URL` | `/docker-model-runner` | Base URL for Docker Model Runner API |
| `DOCKER_MODEL_RUNNER_BASE_URLS` | (empty) | Semicolon-separated list of Docker Model Runner URLs |
| `DOCKER_MODEL_RUNNER_API_BASE_URL` | `http://localhost:11435/api` | Full API URL including /api path |
| `USE_DOCKER_MODEL_RUNNER_DOCKER` | `false` | Enable bundled Docker Model Runner in container |

### Multiple Instances

Configure multiple Docker Model Runner instances for load balancing:

```bash
docker run -d \
  -e DOCKER_MODEL_RUNNER_BASE_URLS="http://runner1:11435;http://runner2:11435;http://runner3:11435" \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

### Advanced Configuration

Configure instance-specific settings via the Admin Panel or API:

```json
{
  "0": {
    "enable": true,
    "key": "optional-api-key",
    "prefix_id": "runner1",
    "tags": ["fast", "gpu"],
    "model_ids": ["llama3:8b", "codellama:13b"],
    "connection_type": "docker"
  }
}
```

## API Endpoints

All endpoints are available under `/docker-model-runner` prefix:

### Health Check
- `GET /docker-model-runner/` - Check service status

### Configuration
- `GET /docker-model-runner/config` - Get current configuration (admin only)
- `POST /docker-model-runner/config/update` - Update configuration (admin only)

### Models
- `GET /docker-model-runner/api/tags` - List available models
- `GET /docker-model-runner/api/tags/{url_idx}` - List models from specific instance
- `GET /docker-model-runner/api/ps` - List loaded models
- `GET /docker-model-runner/api/version` - Get version information
- `POST /docker-model-runner/api/pull` - Pull a model
- `POST /docker-model-runner/api/show` - Show model information

### Inference
- `POST /docker-model-runner/api/chat` - Chat completion (streaming/non-streaming)
- `POST /docker-model-runner/api/generate` - Generate completion
- `POST /docker-model-runner/api/embeddings` - Generate embeddings

## Usage Examples

### Pull a Model

```bash
curl http://localhost:3000/docker-model-runner/api/pull \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "llama3:8b"
  }'
```

### Chat Completion

```bash
curl http://localhost:3000/docker-model-runner/api/chat \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "llama3:8b",
    "messages": [
      {
        "role": "user",
        "content": "Why is the sky blue?"
      }
    ],
    "stream": false
  }'
```

### Generate Embeddings

```bash
curl http://localhost:3000/docker-model-runner/api/embeddings \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "nomic-embed-text",
    "prompt": "Why is the sky blue?"
  }'
```

## Comparison with Ollama

Docker Model Runner provides equivalent functionality to Ollama with these advantages:

| Feature | Ollama | Docker Model Runner |
|---------|--------|---------------------|
| Ollama API Compatibility | ✅ | ✅ |
| Bundled in Docker | ✅ | ✅ |
| Multiple Instances | ✅ | ✅ |
| Model Management | ✅ | ✅ |
| Chat Completions | ✅ | ✅ |
| Embeddings | ✅ | ✅ |
| Custom Port | Manual | ✅ Built-in (11435) |
| Load Balancing | ✅ | ✅ |
| Access Control | Via Open WebUI | ✅ Integrated |
| Connection Type Tag | ❌ | ✅ |

## Troubleshooting

### Docker Model Runner Not Starting

Check if the service is enabled:
```bash
docker exec -it open-webui ps aux | grep ollama
```

Verify environment variables:
```bash
docker exec -it open-webui env | grep DOCKER_MODEL_RUNNER
```

### Connection Refused

Ensure the correct port is configured:
- Bundled: `http://localhost:11435`
- External: `http://host.docker.internal:11435`
- Kubernetes: `http://docker-model-runner-service.open-webui.svc.cluster.local:11435`

### Models Not Loading

Check Docker Model Runner logs:
```bash
docker exec -it open-webui journalctl -u ollama
```

Verify model pull status:
```bash
curl http://localhost:3000/docker-model-runner/api/tags
```

## Kubernetes Deployment

Deploy Docker Model Runner in Kubernetes:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: docker-model-runner
  namespace: open-webui
spec:
  replicas: 2
  selector:
    matchLabels:
      app: docker-model-runner
  template:
    metadata:
      labels:
        app: docker-model-runner
    spec:
      containers:
      - name: ollama
        image: ollama/ollama:latest
        ports:
        - containerPort: 11434
        env:
        - name: OLLAMA_HOST
          value: "0.0.0.0:11434"
---
apiVersion: v1
kind: Service
metadata:
  name: docker-model-runner-service
  namespace: open-webui
spec:
  selector:
    app: docker-model-runner
  ports:
  - port: 11435
    targetPort: 11434
```

Configure Open WebUI to use it:
```yaml
env:
  - name: DOCKER_MODEL_RUNNER_BASE_URL
    value: "http://docker-model-runner-service:11435"
```

## Security Considerations

1. **API Keys**: Use API keys to secure Docker Model Runner endpoints
2. **Network Isolation**: Run Docker Model Runner in isolated networks
3. **Access Control**: Configure model-level access control
4. **TLS/SSL**: Use HTTPS for production deployments
5. **Resource Limits**: Set memory and CPU limits in Kubernetes/Docker

## Performance Tuning

### GPU Support

Enable CUDA for GPU acceleration:

```bash
docker build \
  --build-arg USE_CUDA=true \
  --build-arg USE_DOCKER_MODEL_RUNNER=true \
  -t open-webui-gpu .
```

### Load Balancing

Configure multiple instances for better performance:

```bash
DOCKER_MODEL_RUNNER_BASE_URLS="http://runner1:11435;http://runner2:11435"
```

### Model Caching

Keep frequently used models loaded:

```bash
curl http://localhost:3000/docker-model-runner/api/generate \
  -d '{
    "model": "llama3:8b",
    "prompt": "",
    "keep_alive": -1
  }'
```

## Support

For issues and questions:
- Open an issue on [GitHub](https://github.com/open-webui/open-webui)
- Join our [Discord](https://discord.gg/5rJgQTnV4s)
- Check [Documentation](https://docs.openwebui.com/)

## License

Docker Model Runner integration is part of Open WebUI and follows the same MIT License.
