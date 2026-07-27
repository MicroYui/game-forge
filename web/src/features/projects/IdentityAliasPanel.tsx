import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { useState } from "react";

import { createMutationIntent } from "../../api/csrf";
import { adaptGraphItems, graphFactDisplayName } from "../../components/kg";
import { StatePanel } from "../../components/ui";
import { projectsApi, type GraphPage, type Project, type ProjectsApi } from "./api";

/**
 * Declare that two names mean one thing.
 *
 * Writing-form differences — `air.quality` vs `air_quality` — the system already
 * resolves on its own. 岩王帝君 and 钟离 share no characters, so no rule can: a
 * person has to say it. Once said it is applied deterministically to every later
 * extraction, with no model in the decision.
 */
export function IdentityAliasPanel({
  api = projectsApi,
  graph,
  project,
  projectEtag,
}: {
  api?: ProjectsApi;
  graph: GraphPage | undefined;
  project: Project;
  projectEtag: string;
}) {
  const queryClient = useQueryClient();
  const [alias, setAlias] = useState("");
  const [entityId, setEntityId] = useState("");
  const aliasQuery = useQuery({
    queryFn: () => api.listIdentityAliases(project.project_id),
    queryKey: ["project-identity-aliases", project.project_id],
    retry: false,
  });
  const declare = useMutation({
    mutationFn: (variables: { alias: string; canonicalEntityId: string }) =>
      api.declareIdentityAlias(
        project.project_id,
        {
          alias: variables.alias,
          canonical_entity_id: variables.canonicalEntityId,
          expected_project_revision: project.revision,
          request_schema_version: "project-identity-alias-declare-request@1",
        },
        createMutationIntent(),
        projectEtag,
      ),
    onSuccess: () => {
      setAlias("");
      setEntityId("");
      void queryClient.invalidateQueries({
        queryKey: ["project-identity-aliases", project.project_id],
      });
    },
  });

  const entities = adaptGraphItems(graph?.items ?? []).filter((fact) => fact.kind === "entity");
  const declared = (aliasQuery.data ?? []).filter((item) => item.status === "active");
  const nameOf = (id: string) =>
    entities.find((entity) => entity.id === id)
      ? graphFactDisplayName(entities.find((entity) => entity.id === id)!)
      : id;

  return (
    <section aria-labelledby="project-alias-title" className="gf-project-overview__aliases">
      <header>
        <h2 id="project-alias-title">
          <Link2 aria-hidden="true" size={18} /> 同一个东西的不同叫法
        </h2>
        <p>
          写法不同（例如点号、下划线、大小写）系统会自己合并。像「岩王帝君」和「钟离」这种字面毫无关系的称呼，
          需要你说一次；说过之后，以后每份策划案都会自动指向同一个内容。
        </p>
      </header>

      {entities.length === 0 ? (
        <p className="gf-project-overview__aliases-empty">
          这个游戏还没有内容，先发布首个内容版本，才能把别的叫法指过去。
        </p>
      ) : (
        <form
          className="gf-project-overview__alias-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!alias.trim() || !entityId) return;
            declare.mutate({ alias: alias.trim(), canonicalEntityId: entityId });
          }}
        >
          <label>
            还有一个叫法
            <input
              maxLength={256}
              onChange={(event) => setAlias(event.target.value)}
              placeholder="例如：岩王帝君"
              value={alias}
            />
          </label>
          <label>
            指的是
            <select onChange={(event) => setEntityId(event.target.value)} value={entityId}>
              <option value="">请选择这个游戏里的内容</option>
              {entities.map((entity) => (
                <option key={entity.id} value={entity.id}>
                  {graphFactDisplayName(entity)}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!alias.trim() || !entityId || declare.isPending} type="submit">
            {declare.isPending ? "正在记录…" : "记住这个叫法"}
          </button>
        </form>
      )}

      {declare.error ? (
        <StatePanel
          description="这个叫法没能记下来；确认它指向的内容仍在当前版本里，然后重试。"
          state="error"
          title="没能记住这个叫法"
        />
      ) : null}

      {declared.length > 0 ? (
        <ul className="gf-project-overview__alias-list">
          {declared.map((item) => (
            <li key={item.alias_id}>
              <strong>{item.alias}</strong>
              <span>就是</span>
              <strong>{nameOf(item.canonical_entity_id)}</strong>
            </li>
          ))}
        </ul>
      ) : (
        <p className="gf-project-overview__aliases-empty">还没有记下别的叫法。</p>
      )}
    </section>
  );
}
