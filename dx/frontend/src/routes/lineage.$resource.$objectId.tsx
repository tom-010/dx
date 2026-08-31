import { createFileRoute, Link } from "@tanstack/react-router";
import type { JSX } from "react";
import { useGetLineage } from "@/api/history/history";
import type { EdgeOut, GraphOut, NodeOut } from "@/api/model";
import { errorMessage } from "@/lib/custom-fetch";

export const Route = createFileRoute("/lineage/$resource/$objectId")({
  component: LineagePage,
});

// Layout constants. One row per generation, nodes spread across it.
const NODE_WIDTH = 176;
const NODE_HEIGHT = 52;
const GAP_X = 32;
const GAP_Y = 96;
const PADDING = 24;

type Placed = NodeOut & { x: number; y: number };

/**
 * Lay the graph out in rows by generation: sources above, derived below, siblings alongside.
 * The backend already resolved the generation of every node, so this is pure arithmetic —
 * no layout library, and no dependency to justify for one screen.
 */
function place(nodes: NodeOut[]): {
  placed: Placed[];
  width: number;
  height: number;
} {
  const generations = [...new Set(nodes.map((node) => node.depth))].sort(
    (a, b) => a - b,
  );
  const rows = generations.map((generation) =>
    nodes.filter((node) => node.depth === generation),
  );
  const widest = Math.max(1, ...rows.map((row) => row.length));
  const width = widest * NODE_WIDTH + (widest - 1) * GAP_X + PADDING * 2;

  const placed = rows.flatMap((row, rowIndex) => {
    const rowWidth = row.length * NODE_WIDTH + (row.length - 1) * GAP_X;
    const startX = (width - rowWidth) / 2;
    return row.map((node, index) => ({
      ...node,
      x: startX + index * (NODE_WIDTH + GAP_X),
      y: PADDING + rowIndex * GAP_Y,
    }));
  });

  return {
    placed,
    width,
    height: PADDING * 2 + (rows.length - 1) * GAP_Y + NODE_HEIGHT,
  };
}

function LineagePage(): JSX.Element {
  const { resource, objectId } = Route.useParams();
  const lineage = useGetLineage(resource, objectId);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-semibold text-2xl">Lineage</h1>
        <p className="text-muted-foreground text-sm">
          What this was built from, and what was built from it. An arrow points
          from a source to the thing derived from it and is labelled with the{" "}
          <em>version</em> that was read — so it keeps saying the truth after
          the source is edited.
        </p>
      </div>

      {lineage.isPending && (
        <p className="text-muted-foreground">Loading lineage...</p>
      )}
      {lineage.isError && (
        <p className="text-destructive">
          Failed to load lineage: {errorMessage(lineage.error)}
        </p>
      )}
      {lineage.isSuccess && lineage.data.edges.length === 0 && (
        <p className="text-muted-foreground">
          Nothing was derived from this yet, and it was not derived from
          anything. Fork, split or merge a note to build a graph.
        </p>
      )}
      {lineage.isSuccess && lineage.data.edges.length > 0 && (
        <GraphView graph={lineage.data} resource={resource} />
      )}
    </div>
  );
}

function GraphView({
  graph,
  resource,
}: {
  graph: GraphOut;
  resource: string;
}): JSX.Element {
  const { placed, width, height } = place(graph.nodes);
  const byId = new Map(placed.map((node) => [node.object_id, node]));
  const stale = graph.edges.filter((edge) => edge.is_stale).length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-4 text-muted-foreground text-xs">
        <Legend className="bg-foreground" label="This one" />
        <Legend className="bg-muted-foreground/40" label="Related" />
        <span className="flex items-center gap-2">
          <svg width="28" height="8" aria-hidden="true">
            <title>stale edge</title>
            <line
              x1="0"
              y1="4"
              x2="28"
              y2="4"
              className="stroke-destructive"
              strokeWidth="2"
              strokeDasharray="4 3"
            />
          </svg>
          Source changed since ({stale})
        </span>
      </div>

      {/* Wide graphs scroll inside this box rather than pushing the page sideways. */}
      <div className="overflow-x-auto rounded-lg border p-2">
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`Lineage graph with ${graph.nodes.length} nodes`}
        >
          <title>Lineage graph</title>
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path
                d="M 0 0 L 10 5 L 0 10 z"
                className="fill-muted-foreground"
              />
            </marker>
          </defs>

          {graph.edges.map((edge) => (
            <EdgeLine
              key={`${edge.source_id}-${edge.target_id}`}
              edge={edge}
              from={byId.get(edge.source_id)}
              to={byId.get(edge.target_id)}
            />
          ))}

          {placed.map((node) => (
            <NodeBox
              key={node.object_id}
              node={node}
              isRoot={node.object_id === graph.root_id}
              resource={resource}
            />
          ))}
        </svg>
      </div>
    </div>
  );
}

function Legend({
  className,
  label,
}: {
  className: string;
  label: string;
}): JSX.Element {
  return (
    <span className="flex items-center gap-2">
      <span className={`size-3 rounded-sm ${className}`} />
      {label}
    </span>
  );
}

function EdgeLine({
  edge,
  from,
  to,
}: {
  edge: EdgeOut;
  from: Placed | undefined;
  to: Placed | undefined;
}): JSX.Element | null {
  if (!from || !to) return null;

  const x1 = from.x + NODE_WIDTH / 2;
  const y1 = from.y + NODE_HEIGHT;
  const x2 = to.x + NODE_WIDTH / 2;
  const y2 = to.y;

  return (
    <g>
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        className={
          edge.is_stale ? "stroke-destructive" : "stroke-muted-foreground"
        }
        strokeWidth="1.5"
        strokeDasharray={edge.is_stale ? "4 3" : undefined}
        markerEnd="url(#arrow)"
      />
      <text
        x={(x1 + x2) / 2}
        y={(y1 + y2) / 2 - 4}
        textAnchor="middle"
        className="fill-muted-foreground text-[10px]"
      >
        v{edge.source_version}
      </text>
    </g>
  );
}

function NodeBox({
  node,
  isRoot,
  resource,
}: {
  node: Placed;
  isRoot: boolean;
  resource: string;
}): JSX.Element {
  return (
    <g>
      <rect
        x={node.x}
        y={node.y}
        width={NODE_WIDTH}
        height={NODE_HEIGHT}
        rx="8"
        className={
          isRoot
            ? "fill-foreground stroke-foreground"
            : "fill-background stroke-border"
        }
        strokeWidth="1.5"
      />
      <foreignObject
        x={node.x}
        y={node.y}
        width={NODE_WIDTH}
        height={NODE_HEIGHT}
      >
        <Link
          to="/lineage/$resource/$objectId"
          params={{ resource, objectId: node.object_id }}
          className={`flex h-full flex-col justify-center gap-0.5 px-3 ${
            isRoot ? "text-background" : "text-foreground"
          }`}
        >
          <span className="truncate font-medium text-xs">{node.label}</span>
          <span
            className={`text-[10px] ${isRoot ? "opacity-70" : "text-muted-foreground"}`}
          >
            v{node.version}
            {node.deleted && " · deleted"}
          </span>
        </Link>
      </foreignObject>
    </g>
  );
}
