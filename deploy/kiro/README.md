# Replicar o agents-team em outra máquina / conta do KiroCrew

Tudo que o agents-team precisa fora do repositório é recriado a partir deste diretório.
Nenhum segredo fica aqui: as credenciais são preenchidas na máquina de destino.

| Arquivo | Vai para | Observação |
|---|---|---|
| `config.yml` | `~/.agents-team/config.yml` | modelos por agente e fase; os ids precisam existir na conta (`kiro-cli chat --list-models`) |
| `gitlab-mcp.env.example` | `~/.config/kirocrew/gitlab-mcp.env` (chmod 600) | preencher com o PAT da conta de serviço |
| `mcp/*.json` | `mcpServers` em `~/.kiro/agents/kirocrew.json` | GitLab em modo **somente leitura**; Atlassian via OAuth (`mcp-remote`) |
| `profiles/<repo-id>/` | `~/.agents-team/repos/<repo-id>/discovery/` | perfil pronto; dispensa uma nova discovery |
| — | `~/.kiro/agents/agents-team.json`, `uncle-iroh.json` | gerados pelo instalador |

Não são exportados: cartões, worktrees e `inbox` (estado de execução de cada máquina).

## Pré-requisitos

- `git`, `uv`, `kiro-cli` >= 2.19 logado na conta Kiro que vai pagar os créditos
- KiroCrew instalado (para os agentes do dashboard); a CLI `agents-team` funciona sem ele
- Node via `nvm` ou no PATH (os MCP sobem com `npx`)
- **VPN da GAS ativa**: `gitlab.gastecnologia.com.br` só responde dentro dela
- Acesso SSH ao GitLab para clonar os repositórios

## Passo a passo

```bash
git clone <remote do agents-team> ~/agents-team && cd ~/agents-team
uv tool install -e .                 # coloca `agents-team` no PATH
agents-team install                  # idempotente: não sobrescreve nada que já exista
$EDITOR ~/.config/kirocrew/gitlab-mcp.env   # preencher o PAT (escopo read_api)
# reiniciar o KiroCrew para carregar os MCP; na primeira chamada à Atlassian, autorizar o OAuth
agents-team doctor                   # tudo "ok" antes de rodar um cartão

# para cada repositório com perfil em profiles/:
git clone git@gitlab.gastecnologia.com.br:Gasbiz/integration-tests.git ~/repos/integration-tests
agents-team discover-apply --repo ~/repos/integration-tests
```

Depois, abrir uma vez a página **Agents** do dashboard para registrar `agents-team` e `uncle-iroh`.

Opções do `install`: `--force` (reescreve `config.yml` e perfis), `--force-mcp` (substitui entradas MCP
existentes; faz backup do `kirocrew.json` antes), `--offline` (pula o teste de rede).

## Atualizar os perfis

Depois de ajustar o `.agents-team.yml` de um repositório na sua máquina:

```bash
agents-team export-profile --repo ~/topaz-repos/integration-tests --clone "git clone <remote>"
git diff deploy/kiro/profiles/    # revisar: tirar ajustes que eram de um cartão só
```

Regra: o perfil descreve o repositório, não um cartão. Ajustes como `branches.base: <branch do MR>`,
`official: "{card}-review"` e `workflow.sdet: skip` (modo revisão) são por cartão e não entram aqui.
Um teste (`tests/test_deploy.py`) falha se um perfil trouxer `sdet: skip` ou texto de cartão nas convenções.

## Conta de serviço

Mesmos passos, logado como o usuário de serviço, com estas diferenças:

- **GitLab:** PAT do usuário de serviço com `read_api` enquanto o serviço não abre MRs. Se passar a
  abrir MR draft, `api` com o usuário só como *Developer* e a branch de destino protegida.
- **Jira:** autorizar o OAuth da Atlassian com o usuário de serviço.
- **Kiro:** `kiro-cli login` com a conta de serviço; os créditos dos cartões saem dela.
- **Gateway:** rodar como serviço do sistema (`kirocrew service`) em uma máquina dentro da VPN.
