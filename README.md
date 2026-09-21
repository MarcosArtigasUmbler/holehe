# holehe API

API HTTP em cima do [holehe](https://github.com/megadose/holehe): recebe um e-mail e
retorna em quais plataformas ele possui conta.

## Rodar local (Docker)

```bash
docker compose up -d --build
```

- Swagger: http://localhost:8000/docs
- Health:  http://localhost:8000/health

## Endpoints

| Método | Rota       | Descrição                                   |
|--------|------------|---------------------------------------------|
| GET    | `/health`  | Status e número de módulos carregados        |
| GET    | `/modules` | Lista dos sites verificados                  |
| GET    | `/check?email=…` | Scan rápido via query string           |
| POST   | `/check`   | Scan com opções (body JSON)                  |

```bash
curl "http://localhost:8000/check?email=someone@example.com&only_used=true"
```

```bash
curl -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"email":"someone@example.com","timeout":10,"no_password_recovery":true,"only_used":true}'
```

Resposta (resumida):

```json
{
  "email": "someone@example.com",
  "elapsed_seconds": 8.4,
  "summary": {"checked": 121, "used": 3, "not_used": 100, "rate_limited": 15, "errors": 3},
  "used": ["github.com", "spotify.com", "twitter.com"],
  "results": [
    {"name": "github", "domain": "github.com", "method": "register", "exists": true,
     "rate_limited": false, "error": false, "frequent_rate_limit": false,
     "email_recovery": null, "phone_number": null, "others": null}
  ]
}
```

## Configuração

Copie `.env.example` para `.env`. Variáveis:

- `API_KEY`: se preenchida, `/check` e `/modules` exigem o header `X-API-Key`.
- `MAX_CONCURRENT_SCANS`: scans simultâneos aceitos (cada um abre ~120 conexões de saída).
- `HOLEHE_TIMEOUT`: timeout por site em segundos (default 10).
- `HOLEHE_MODULE_CONCURRENCY`: sites verificados em paralelo dentro de um scan.

## Observações

- `no_password_recovery=true` evita módulos (adobe, mail.ru, ok.ru, samsung) que disparam
  e-mail de recuperação de senha para o alvo.
- Sites com `rate_limited=true` bloquearam a verificação; o IP de saída influencia muito.
  Em produção, considere um proxy de saída rotativo se o volume for alto.
- holehe é GPLv3.

## Produção (EC2 + GitHub Actions)

Pipeline em `.github/workflows/deploy.yml`, disparo manual (Actions → Build & Deploy → Run workflow):

1. Builda a imagem e publica em `ghcr.io/<owner>/holehe-api:latest` e `:<sha>`.
2. Via SSH no EC2: pull, recria o container `holehe-api` em `127.0.0.1:8091`, espera o healthcheck.
3. O nginx do host (`deploy/nginx/holehe.artigas.app.conf`) publica em https://holehe.artigas.app com TLS do certbot.

Secrets do repositório: `EC2_HOST`, `EC2_USER`, `SSH_PRIVATE_KEY`, `GHCR_READ_TOKEN` (PAT com `read:packages`), `API_KEY`.
Variáveis opcionais: `HOST_PORT` (default 8091), `MAX_CONCURRENT_SCANS`, `HOLEHE_TIMEOUT`.

Rollback: `docker run` da tag `:<sha>` anterior no EC2, mesmo comando do workflow.
