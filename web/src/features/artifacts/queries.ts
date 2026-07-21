import { queryOptions } from "@tanstack/react-query";

import { artifactDetailApi, type ArtifactDetailApi } from "./api";

export function artifactDetailQueryOptions(artifactId: string, api: ArtifactDetailApi = artifactDetailApi) {
  return queryOptions({
    queryFn: () => api.load(artifactId),
    queryKey: ["artifact-detail", artifactId] as const,
    retry: false,
  });
}
