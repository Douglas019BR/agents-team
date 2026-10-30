# agents-team: documento de design

Status: rascunho v0.1 (2026-10-30)
Autores: Douglas Sermarini + Kiro

Time de agentes de engenharia (SDET, dev, revisor, QA, sec e professor) portável entre harnesses
(Kiro, Claude Code, Codex, Cursor etc.). Este documento registra as decisões de design antes da
implementação de qualquer agente.

## 1. Princípios

1. **Tudo local.** Os agentes trabalham só na máquina do usuário. Nada vai para o GitLab sem uma ação
   humana. No Jira, só o orquestrador escreve (seção 7).
2. **Push e MR são sempre humanos.** Os agentes apenas commitam localmente.
3. **Portabilidade.** As definições canônicas são neutras. Cada harness recebe adaptadores finos
   gerados a partir delas.
4. **Menor privilégio por papel.**
5. **Diversidade de modelos.** O modelo é configurável por papel, e o revisor usa um modelo diferente
   do dev, para evitar viés e o reforço de erros entre agentes.
6. **Contratos versionados.** Os schemas dos artefatos seguem semver. Uma mudança incompatível exige
   major.

## 2. O time

| Nome        | Papel     | Escreve                              | Commita | Invocação          |
|-------------|-----------|--------------------------------------|---------|--------------------|
| Aang        | Dev       | `plan.md`, código de produção, step definitions | sim | orquestrador |
| Toph        | SDET      | testes (`.feature`, testes unitários) | sim     | orquestrador       |
| Sokka       | Revisor   | achados (plano e código)             | não     | orquestrador       |
| Katara      | QA        | achados, relatórios de execução      | não     | orquestrador       |
| Zuko        | Sec       | achados, relatórios de scanners      | não     | orquestrador       |
| Uncle Iroh  | Professor | nada (só leitura)                    | não     | **só pelo humano** |

Os nomes são configuráveis na definição de cada agente e não ficam fixos no código.

### 2.0 Modelos

Configuráveis por agente e por fase em `~/.agents-team/config.yml`. Padrão:

| Agente     | Fase        | Modelo              |
|------------|-------------|---------------------|
| Aang       | `plan`      | `claude-opus-5.5`   |
| Aang       | `implement` | `glm-5`             |
| Toph       | todas       | `claude-sonnet-5.5` |
| Sokka      | todas       | `gpt-5.6-terra`     |
| Katara     | `qa`        | `claude-haiku-5.5`  |
| Zuko       | `sec`       | `gpt-5.6-luna`      |
| Uncle Iroh | `teach`     | `claude-opus-5.5`   |

Regras: o revisor nunca usa o mesmo modelo do dev (um teste garante isso); o `auto` não é usado,
porque quebra a garantia de diversidade e a rastreabilidade dos evals. Riscos a acompanhar nos evals:
`glm-5` na implementação (número de iterações) e `gpt-5.6-luna` na triagem de segurança (falsos
negativos).

### 2.1 Regras de propriedade

- **Implementar sistema:** o Aang não altera testes escritos pela Toph. Se discordar de um teste,
  registra um achado, e a Toph corrige.
- **BDD (behave):** o Aang não altera os arquivos `.feature` da Toph. Se um cenário não puder ser
  implementado como está escrito, ele volta para a Toph.
- O Sokka verifica essas regras pelo diff.

## 3. Fluxos (roteados pelo orquestrador)

O orquestrador classifica o cartão do Jira pelo tipo de issue ou pelas labels. Se o cartão for
ambíguo, classifica com o LLM e pede confirmação humana antes de começar.

Todos os fluxos começam pela **skill de discovery**: se o repositório-alvo não tiver
`.agents-team.yml`, ela detecta a stack, gera o arquivo e o adiciona a `.git/info/exclude`.

### 3.1 Implementar sistema

```
Aang (plan.md) → Sokka (revisa o plano; no máximo 1 revisão) [→ humano, se plan_approval: human]
  → Toph (testes que falham, contra as interfaces do plano)
  → Aang (implementa até passar) → Sokka → Katara ∥ Zuko
  → consolidação → [request_changes? volta ao Aang ou à Toph, até 3 iterações]
  → montagem da branch oficial → humano revisa, faz push e abre o MR
```

### 3.2 Implementar casos BDD (behave)

```
Toph (.feature em Gherkin a partir dos critérios de aceite)
  → Aang (step definitions, helpers)
  → Sokka (fidelidade ao cartão, reuso de steps, legibilidade)
  → Katara (roda contra o sistema, procura instabilidade e falsos positivos)
    ∥ Zuko (segredos nos dados de teste, credenciais nos fixtures)
  → consolidação → branch oficial → humano
```

### 3.3 Revisão de MRs de pares

Esta é a revisão que você faz nos MRs dos colegas. Os agentes preparam um rascunho. Você decide o que
publicar.

```
entrada: URL do MR ou cartão do Jira
  → orquestrador lê o MR via MCP do GitLab (somente leitura) e faz o checkout num worktree local
  → Toph (os testes do MR cobrem os critérios de aceite do cartão?)
  → Sokka → Katara ∥ Zuko
  → rascunho consolidado de comentários (arquivo, linha, severidade, texto sugerido) em arquivo local
  → humano lê, edita e publica no GitLab
```

Neste fluxo nenhum agente commita, e nenhum publica comentário.

### 3.4 Professor (Uncle Iroh)

- Fica fora dos fluxos. Só é invocado por chamada direta do humano.
- Somente leitura: definições dos agentes, workflows, schemas, artefatos dos cartões e o código.
- Explica em alto nível (por que o fluxo existe, como os papéis se relacionam) e em baixo nível
  (o que um artefato, um commit ou uma regra específica fazem).
- **Não salva por padrão.** Só salva quando o humano pede, em `teaching/`. Cada explicação salva
  registra o hash do commit e a versão dos agentes em que foi gerada. Antes de reusar uma explicação,
  o Uncle Iroh compara essas versões e a refaz se estiver desatualizada. Nenhum outro agente lê
  `teaching/`.

## 4. Regras de loop, veto e escalonamento

- No máximo **3 iterações** de correção por cartão. Depois disso, escala para o humano com o histórico.
- Vetos:
  - **Zuko:** bloqueia por severidade (`high` ou `critical`). O limite é configurável.
  - **Katara:** bloqueia se algum teste falhar.
  - **Sokka:** bloqueia por julgamento, com justificativa obrigatória.
- Merge, push e abertura de MR são sempre humanos.

## 5. Git

### 5.1 Branches

- `agents/<CARD>/work`: a branch de trabalho. Pode ter bagunça e `AGENT-NOTE`. **Nunca recebe push.**
- `feature/<CARD>-<slug>`: a branch oficial, montada pelo orquestrador no fim do fluxo.

### 5.2 Commits

- Na branch de trabalho: `<nome>-<cargo>: <mensagem>`, por exemplo
  `toph-sdet: adiciona cenários de token expirado`.
- Na branch oficial: a convenção do repositório-alvo (definida no `.agents-team.yml`), com a autoria
  em trailers:

  ```
  feat(auth): valida token expirado

  Agent-Authors: toph-sdet, aang-dev
  Agent-Reviewed: sokka-reviewer, katara-qa, zuko-sec
  ```

### 5.3 Comentários entre agentes no código

Formato rígido, que dá para achar com grep:

```
// AGENT-NOTE(R-001 sokka-reviewer -> aang-dev): validar null antes do parse
# AGENT-NOTE(S-001 zuko-sec -> aang-dev): não logar o token
```

- **Os revisores não editam arquivos.** O orquestrador transforma cada achado com `file` e `line` em
  um `AGENT-NOTE` e commita como `orchestrator: add review notes (iteration NN)`. Assim Sokka, Katara
  e Zuko ficam com escrita zero no repositório.
- Quem corrige o achado remove a nota no mesmo commit. Para contestar, edita o texto da nota e
  explica no seu artefato.
- Os comentários servem para conversa. Decisões e vereditos ficam nos artefatos (seção 6).
- O orquestrador remove todos os `AGENT-NOTE` ao montar a branch oficial.
- **Um gate final falha se sobrar qualquer `AGENT-NOTE`** na branch oficial.

### 5.4 Montagem da branch oficial

1. O orquestrador propõe um plano de agrupamento por unidade lógica. Por exemplo: testes e implementação
   do comportamento X num commit, configuração ou migração em outro. As correções das iterações são
   absorvidas no commit lógico a que pertencem.
2. O humano aprova o plano.
3. O orquestrador reescreve a história na branch oficial, roda os gates (testes, `AGENT-NOTE`,
   scanners) e entrega.

## 6. Artefatos e estado

### 6.1 Localização

O estado fica **fora do repositório-alvo**, para não ser commitado por acidente e para ser
compartilhado entre worktrees. A raiz é configurável e o padrão é `~/.agents-team/`.

```
~/.agents-team/
  config.yml                      # modelos por papel, limites globais
  repos/<repo-id>/                # repo-id = slug do remote
    memory/
      lessons.md                  # lições do repositório, reaproveitadas entre cartões
      conventions.md
    cards/<CARD>/                 # CARD = chave do Jira (PROJ-123)
      card.md                     # snapshot do cartão lido do Jira
      state.json                  # fluxo, fase, iteração, vereditos
      plan.md                     # Aang (fase plan)
      plan-review.json            # Sokka (stage plan, iteration 0)
      test-plan.md                # Toph
      iterations/01/
        change-summary.md         # Aang
        review.json               # Sokka
        qa.json                   # Katara
        sec.json                  # Zuko
      grouping-plan.md            # plano de agrupamento dos commits
      final-report.md
    reviews/<MR-iid>/             # fluxo 3.3
      coverage.json               # Toph (stage coverage)
      review.json, qa.json, sec.json
      draft-comments.md
    teaching/                     # Uncle Iroh, só sob demanda
```

A sessão do harness não é chave de nada, porque é efêmera e depende do harness.

**Idioma:** prompts, código, chaves e enums em inglês. Textos lidos por humanos (`summary`,
`message`, artefatos Markdown, `jira_actions`, rascunho de comentários) saem no idioma do cartão.

### 6.2 `.agents-team.yml` (no repositório-alvo, sem commit)

Gerado pela skill de discovery e excluído via `.git/info/exclude` (não pelo `.gitignore`, que é
versionado). Conteúdo previsto:

```yaml
version: 1
stack: { language: python, test_runner: pytest, bdd: behave }
commands:
  build: ...
  test: pytest
  bdd: behave
  lint: ruff check .
security:
  tools: [semgrep, gitleaks, osv-scanner]   # o que estiver instalado localmente
  block_severity: high
commits:
  convention: conventional
```

### 6.3 Envelope comum dos achados (esboço)

```json
{
  "schema": "agents-team/finding-report@1",
  "card": "PROJ-123",
  "iteration": 1,
  "agent": { "name": "sokka", "role": "reviewer", "model": "..." },
  "verdict": "approve | request_changes | block",
  "findings": [
    {
      "id": "R-001",
      "severity": "info | low | medium | high | critical",
      "category": "correctness | design | test | security | ...",
      "file": "src/auth.py",
      "line": 42,
      "message": "...",
      "suggestion": "...",
      "owner": "aang-dev"
    }
  ]
}
```

## 7. Ferramentas e permissões

- As ferramentas externas chegam via MCP: Jira e GitLab (leitura de MRs, sempre somente leitura).
- **Escrita no Jira: só o orquestrador.** Os agentes não acessam o Jira para escrever. Cada agente
  declara no seu artefato, no campo `jira_actions`, o que precisa ser comentado ou editado. O
  orquestrador consolida essas ações, **mostra o lote ao humano e só aplica depois da confirmação**.
  A escrita acontece por uma invocação do harness com o MCP da Atlassian (sem token novo).

  ```json
  "jira_actions": [
    { "type": "comment", "body": "..." },
    { "type": "update_field", "field": "labels", "add": ["needs-design-review"] }
  ]
  ```
- Os scanners de segurança usados são os que estiverem instalados localmente.
- Permissões por papel, conforme a seção 2: só a Toph e o Aang escrevem e commitam. Os demais apenas leem,
  executam testes e ferramentas, e escrevem artefatos e `AGENT-NOTE`.

## 8. Portabilidade

- **Fonte canônica:** `agents/<papel>.md` (frontmatter YAML com papel, nome, ferramentas permitidas,
  entradas e saídas, seguido das instruções em Markdown).
- Padrões abertos são o alvo quando servem: `AGENTS.md` e Agent Skills (`SKILL.md`).
- `adapters/<harness>/` gera o formato nativo de cada harness a partir da fonte canônica.
- O orquestrador é local, **escrito em Python**, e executa os workflows definidos em arquivo, como
  máquinas de estado. O harness só fornece a execução de cada papel, por meio de um **runner** por
  harness (interface única: papel, prompt, diretório de trabalho e modelo; retorna o resultado e o
  caminho do artefato).

### 8.1 Primeiro adaptador: Kiro / KiroCrew

Baseado no formato de agente do Kiro CLI 2.19 (`~/.kiro/agents/<nome>.json`) e no registro de agentes
do KiroCrew (`kirocrew agent create`).

| Canônico (`agents/<papel>.md`) | Kiro (`<nome>.json`)                                  |
|--------------------------------|-------------------------------------------------------|
| corpo Markdown (instruções)    | `prompt: file://.../<nome>-prompt.md`                 |
| `tools`                        | `tools`, `allowedTools`                               |
| caminhos graváveis             | `toolsSettings.write.allowedPaths` / `deniedPaths`    |
| comandos permitidos            | `toolsSettings.shell.allowedCommands`, `denyByDefault` |
| `model`                        | `model`                                               |
| servidores MCP                 | `mcpServers`                                          |

- **Um agente Kiro por (agente, fase)**, gerado a cada execução em `<worktree>/.kiro/agents/at-<agente>-<fase>.json`
  (agente local do Kiro). Os escopos dependem do repositório-alvo e do cartão, então não há instalação
  global dos agentes do fluxo. `.kiro/` entra no `.git/info/exclude`. Só o Uncle Iroh, chamado pelo
  humano, é instalado em `~/.kiro/agents/` e registrado no KiroCrew.
- **Runner Kiro:** `kiro-cli chat --no-interactive --agent at-<agente>-<fase> --model <modelo> "<tarefa>"`,
  executado no worktree do cartão, **sem `--trust-tools`**.
- **Comportamento verificado no kiro-cli 2.19 (modo não interativo):**
  - escrita fora de `allowedPaths` é rejeitada, sem pedir aprovação; `deniedPaths` vence `allowedPaths`;
  - com `denyByDefault`, um comando que não casa inteiro com `allowedCommands` é rejeitado; as regex
    geradas proíbem `; & | ` $ < >` e quebra de linha, o que bloqueia encadeamento, redirecionamento e
    substituição (`ls; touch x`, `ls $(...)`, `ls && ...` foram rejeitados no teste);
  - `allowedTools` e `--trust-tools` passam por cima das regras de caminho e comando, por isso `write`
    e `shell` nunca entram neles;
  - uma chamada rejeitada cancela as outras do mesmo lote paralelo; os prompts pedem uma chamada por vez.
- Limitação: o Kiro injeta recursos padrão em agentes customizados (`AGENTS.md`, `README.md`, steering
  global e do projeto). A opção `chat.disableInheritingDefaultResources` é global, então não é usada;
  os prompts dos agentes têm precedência declarada sobre esses recursos.
- Os limites de escrita da Toph (só caminhos de teste) e do Aang (sem tocar nos testes da Toph) são
  impostos por `toolsSettings.write`. A verificação do Sokka pelo diff continua como segunda camada.
- **Entrada no KiroCrew:** um agente `agents-team` no dashboard recebe o pedido em linguagem natural
  ("implementa PROJ-123", "revisa este MR") e chama o CLI do orquestrador.
- O fluxo 3.3 é construído do zero, com o MCP do GitLab em modo somente leitura
  (`GITLAB_READ_ONLY_MODE=true`). O bot `@devops.n8n` do GitLab é um sistema à parte e não faz parte
  do agents-team.

## 9. Avaliação

`evals/` guarda cenários com problemas plantados: bugs, vulnerabilidades conhecidas, testes fracos
e `AGENT-NOTE` esquecidos. Eles rodam no CI do próprio agents-team para detectar regressão quando um
prompt ou um modelo mudar.

## 10. Layout do repositório (proposto)

```
agents-team/
  README.md
  AGENTS.md
  docs/design.md
  agents/            _shared.md, aang.md, toph.md, sokka.md, katara.md, zuko.md, uncle-iroh.md
  skills/discovery/SKILL.md
  workflows/         implement-system.yml, bdd-behave.yml, peer-review.yml
  schemas/           common, finding-report, state, agents-team-config, team-config, agent-definition
  orchestrator/
  adapters/          kiro/, claude-code/, ...
  evals/
```

## 11. Questões em aberto

1. Licença do repositório.

## 12. Registro de decisões

- 2026-10-30: fluxo 3.3 confirmado; escrita no Jira só pelo orquestrador, via `jira_actions`;
  orquestrador em Python; primeiro adaptador para Kiro/KiroCrew.
- 2026-10-30: `jira_actions` só são aplicadas após confirmação humana, via MCP da Atlassian;
  orquestrador com código próprio mínimo, sem framework; fluxo 3.3 construído do zero (sem reaproveitar
  o agente `mr-reviewer`; o bot `@devops.n8n` é um sistema à parte).
- 2026-10-30: Aang planeja (`plan.md`) antes da Toph, e o Sokka revisa o plano; modelos por agente e
  por fase (seção 2.0); revisores não editam arquivos, o orquestrador gera os `AGENT-NOTE`; prompts em
  inglês, textos para humanos no idioma do cartão.
- 2026-10-30: adaptador Kiro gera um agente local por (agente, fase); permissões verificadas no
  kiro-cli 2.19 em modo não interativo (seção 8.1).
- Ordem de implementação: schemas → definições canônicas dos agentes → adaptador Kiro → skill de
  discovery → orquestrador → evals.
