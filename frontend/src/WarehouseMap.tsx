import { useEffect, useRef, useState } from 'react'

import {
  getMapInfo,
  MAP_IMAGE_URL,
  type MapInfoDto,
  type OmxDeviceDto,
  type DockingStationDto,
} from './api/fmsApi'

import type { MapEdge, MapPoint } from './hooks/useRouteGraph'
import type { ManagedRobot } from './hooks/useRobotFleet'
import './warehouse-map.css'


type RobotId = 'R-01' | 'R-02' | 'R-03'
type Point = { x: number; y: number }

type Props = {
  nodes: Record<string, MapPoint>
  edges: MapEdge[]
  layers: { nodeEdge: boolean; route: boolean; station: boolean; robotId: boolean }
  selectedRobot: RobotId | null
  onSelect: (id: RobotId) => void
  picking: boolean
  targetNode?: string
  onPick: (id: string) => void
  graphLoading?: boolean
  graphError?: string | null
  visibleRobotIds?: RobotId[]
  robotStates?: ManagedRobot[]
  omxDevices?: OmxDeviceDto[]
  dockingStations?: DockingStationDto[]
}

const ROBOT_COLORS: Record<RobotId, string> = {
  'R-01': '#F52525',
  'R-02': '#E5A53A',
  'R-03': '#36BD8A',
}

export default function WarehouseMap({
  nodes,
  edges,
  layers,
  selectedRobot,
  onSelect,
  picking,
  targetNode,
  onPick,
  graphLoading = false,
  graphError = null,
  visibleRobotIds = [],
  robotStates = [],
  omxDevices = [],
  dockingStations,
}: Props) {
  const host = useRef<HTMLDivElement>(null)
  const drag = useRef<{ id: number; x: number; y: number; pan: Point } | null>(null)
  const [size, setSize] = useState({ width: 600, height: 600 })
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 })
  const raw = false
  const [dragging, setDragging] = useState(false)
  const [mapInfo, setMapInfo] = useState<MapInfoDto | null>(null)
  const [mapImageSrc, setMapImageSrc] = useState('')
  // const gridId = useId().replace(/:/g, '')

  useEffect(() => {
    let cancelled = false

    getMapInfo()
      .then(info => {
        if (cancelled) return
        setMapInfo(info)

        // 같은 파일명으로 맵을 교체해도 새로고침 시 새 이미지를 요청
        setMapImageSrc(`${MAP_IMAGE_URL}?v=${Date.now()}`)
      })
      .catch(error => console.error('맵 정보 로드 실패:', error))

    return () => {
      cancelled = true
    }
  }, [])

  const mapWidth = mapInfo?.width ?? 97
  const mapHeight = mapInfo?.height ?? 47

  const viewWidth = (mapWidth + 16) / zoom
  const viewHeight = (mapHeight + 16) / zoom

  const scale = Math.max(
    0.1,
    Math.min(size.width / viewWidth, size.height / viewHeight),
  )
  const px = (n: number) => n / scale
  const adjustZoom = (factor: number) => setZoom(value => Math.max(0.75, Math.min(4, value * factor)))
  const reset = () => { setZoom(1); setPan({ x: 0, y: 0 }) }
  const robotsForMap = robotStates
  .filter(robot =>
    visibleRobotIds.includes(robot.id)
    && robot.hasPose
    && robot.pixelX != null
    && robot.pixelY != null
  )
  .map(robot => ({
    id: robot.id,
    x: robot.pixelX as number,
    y: robot.pixelY as number,
    color: ROBOT_COLORS[robot.id],
    heading: 90 - robot.yaw * 180 / Math.PI,
    // 시뮬레이션 로봇은 가상 Fleet 자체가 연결 주체이므로 항상 활성 표시한다.
    stale: robot.mode === 'real' && !robot.connected,
  }))

  return (
    <div className="warehouse-map" ref={host}>
      <svg
        className="warehouse-map__canvas"
        aria-label="실제 공간 기반 관제 지도"
        role="group"
        viewBox={`${mapWidth / 2 - viewWidth / 2 + pan.x} ${mapHeight / 2 - viewHeight / 2 + pan.y} ${viewWidth} ${viewHeight}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ cursor: dragging ? 'grabbing' : 'grab' }}
        onPointerDown={event => {
          if (event.button !== 0) return
          drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, pan }
          event.currentTarget.setPointerCapture(event.pointerId)
          setDragging(true)
        }}
        onPointerMove={event => {
          const start = drag.current
          if (!start) return
          setPan({
            x: start.pan.x - (event.clientX - start.x) / scale,
            y: start.pan.y - (event.clientY - start.y) / scale,
          })
        }}
        onPointerUp={() => { drag.current = null; setDragging(false) }}
        onPointerCancel={() => { drag.current = null; setDragging(false) }}
        onWheel={event => adjustZoom(event.deltaY < 0 ? 1.12 : 1 / 1.12)}
      >
        
        {mapInfo && (
          <image
            href={mapImageSrc}
            x="0"
            y="0"
            width={mapInfo.width}
            height={mapInfo.height}
            imageRendering="pixelated"
          />
        )}

        {/* GeoJSON 기반 Edge */}
        {!raw && layers.nodeEdge && (
          <g data-testid="navigation-edges" fill="none" stroke="#B9C3CF" strokeWidth={px(2)} strokeLinecap="round">
            {edges.map(edge => {
              const from = nodes[edge.from]
              const to = nodes[edge.to]
              if (!from || !to) return null
              return (
                <path
                  key={edge.id}
                  d={`M${from.x} ${from.y}L${to.x} ${to.y}`}
                />
              )
            })}
          </g>
        )}

        {/* GeoJSON 기반 Node */}
        {!raw && (layers.nodeEdge || picking) && (
          <g data-testid="navigation-nodes">
            {Object.entries(nodes).map(([id, node]) => (
              <g
                key={id}
                role={picking ? 'button' : undefined}
                tabIndex={picking ? 0 : undefined}
                aria-label={picking ? `Node ${id} 목표 선택` : undefined}
                style={{ cursor: picking ? 'crosshair' : 'inherit' }}
                onPointerDown={event => { if (picking) event.stopPropagation() }}
                onClick={() => { if (picking) onPick(id) }}
                onKeyDown={event => {
                  if (picking && (event.key === 'Enter' || event.key === ' ')) {
                    event.preventDefault()
                    onPick(id)
                  }
                }}
              >
                <circle cx={node.x} cy={node.y} r={px(picking ? 13 : 9)} fill="transparent" />
                {targetNode === id && picking && (
                  <circle cx={node.x} cy={node.y} r={px(11)} fill="#2589F5" opacity="0.18" />
                )}
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={px(3.8)}
                  fill={targetNode === id && picking ? '#2589F5' : '#FFF'}
                  stroke="#8192A5"
                  strokeWidth={px(1.3)}
                />
                <text
                  x={node.x + px(7)}
                  y={node.y - px(6)}
                  fontSize={px(9)}
                  fill="#718194"
                  paintOrder="stroke"
                  stroke="#F1F4F7"
                  strokeWidth={px(2)}
                >
                  N{id}
                </text>
              </g>
            ))}
          </g>
        )}

        {/* 로봇별 활성 경로. 도착/정지 telemetry의 route=null이면 자동 해제. */}
        {!raw && layers.route && (
          <g data-testid="robot-routes" pointerEvents="none">
            {robotStates.filter(robot => visibleRobotIds.includes(robot.id) && robot.route).map(robot => (
              <g key={robot.id} data-robot-route={robot.id} data-route-phase={robot.route!.phase}>
                {robot.route!.edge_ids.map((edgeId, index) => {
                  const from = nodes[robot.route!.node_ids[index]]
                  const to = nodes[robot.route!.node_ids[index + 1]]
                  if (!from || !to) return null
                  return <path key={`${edgeId}-${index}`} data-active-edge={edgeId}
                    d={`M${from.x} ${from.y}L${to.x} ${to.y}`} fill="none"
                    stroke={ROBOT_COLORS[robot.id]} strokeWidth={px(5)} strokeLinecap="round" opacity={0.8} />
                })}
                {robot.route!.node_ids.map((nodeId, index) => {
                  const node = nodes[nodeId]
                  if (!node) return null
                  return <circle key={`${nodeId}-${index}`} data-active-node={nodeId}
                    cx={node.x} cy={node.y} r={px(6)} fill={ROBOT_COLORS[robot.id]}
                    stroke="#fff" strokeWidth={px(1.5)} />
                })}
              </g>
            ))}
          </g>
        )}

        {/* OMX 장치 */}
        {!raw && (
          <g data-testid="omx-markers" pointerEvents="none">
            {omxDevices.map(omx => {
              const color = omx.connected ? '#2589F5' : '#9AA4B0'

              return (
                <g
                  key={omx.omx_id}
                  transform={`translate(${omx.pixel_x} ${omx.pixel_y})`}
                >
                  <circle
                    cx={0}
                    cy={0}
                    r={px(8)}
                    fill={color}
                    stroke="#FFFFFF"
                    strokeWidth={px(1.5)}
                  />

                  <text
                    x={0}
                    y={px(0.5)}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize={px(6)}
                    fontWeight="800"
                    fill="#FFFFFF"
                  >
                    OMX
                  </text>

                  <text
                    x={px(12)}
                    y={px(3)}
                    fontSize={px(8)}
                    fontWeight="800"
                    fill={color}
                    paintOrder="stroke"
                    stroke="#FFFFFF"
                    strokeWidth={px(2)}
                  >
                    {omx.omx_id.toUpperCase()}
                  </text>
                </g>
              )
            })}
          </g>
        )}

        {/* 충전 스테이션 왼쪽 빈 공간에 고정한다. 로봇이 이동해도 표시등은 따라가지 않는다. */}
        {!raw && layers.station && dockingStations && (
          <g data-testid="docking-station-lights" pointerEvents="none">
            {[1, 2, 3].map(stationId => {
              const node = nodes[String(stationId - 1)]
              if (!node) return null
              const contact = dockingStations.find(station => station.station === stationId)?.contact ?? 'UNKNOWN'
              const color = contact === 'CONTACT' ? '#22C55E' : contact === 'RELEASED' ? '#EF4444' : '#94A3B8'
              const label = contact === 'CONTACT' ? '접촉 · 충전 표시' : contact === 'RELEASED' ? '미접촉' : '확인 불가'
              return (
                <g key={stationId} transform={`translate(${node.x - 12} ${node.y})`}
                  role="img" aria-label={`충전 스테이션 ${stationId}: ${label}`}
                  data-docking-station={stationId} data-contact={contact}>
                  <title>{`충전 스테이션 ${stationId}: ${label}`}</title>
                  <circle r={1.5} fill="#FFFFFF" stroke="#CBD5E1" strokeWidth={0.2} />
                  <circle r={1.1} fill={color} />
                </g>
              )
            })}
          </g>
        )}

        {/* 실제 로봇 위치 */}
        {!raw && layers.robotId && (
          <g data-testid="robot-markers">
            {robotsForMap.map(robot => {
              const position = { x: robot.x, y: robot.y }
              const color = ROBOT_COLORS[robot.id]
              const selected = selectedRobot === robot.id
              return (
                <g
                  key={robot.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`${robot.id} 로봇 선택`}
                  opacity={robot.stale ? 0.42 : 1}
                  style={{ cursor: 'pointer', filter: robot.stale ? 'grayscale(1)' : 'none' }}
                  onPointerDown={event => event.stopPropagation()}
                  onClick={() => onSelect(robot.id)}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      onSelect(robot.id)
                    }
                  }}
                >
                  {selected && <circle cx={position.x} cy={position.y} r={px(21)} fill="#2589F5" opacity="0.12" />}
                  <circle cx={position.x} cy={position.y + px(2)} r={px(13)} fill="#263545" opacity="0.12" />
                  <circle
                    cx={position.x}
                    cy={position.y}
                    r={px(13)}
                    fill={robot.stale ? '#C7CDD4' : robot.color}
                    stroke={
                      selected ? '#2589F5' : robot.stale ? '#9AA4B0' : robot.color
                    }
                    strokeWidth={px(selected ? 3 : 2.3)}
                  />
                  <path
                    d={`M${position.x} ${position.y - px(9)}v${-px(8)}`}
                    transform={`rotate(${robot.heading} ${position.x} ${position.y})`}
                    stroke={robot.stale ? '#7D8792' : '#273444'}
                    strokeWidth={px(2)}
                    strokeLinecap="round"
                  />
                  <text
                    x={position.x}
                    y={position.y + px(0.5)}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize={px(8.5)}
                    fontWeight="800"
                    fill={robot.stale ? '#69737E' : '#FFFFFF'}
                  >
                    {robot.id}
                  </text>
                </g>
              )
            })}
          </g>
        )}
      </svg>

      <div className="warehouse-map__tools" aria-label="지도 도구">
        <button title="확대" aria-label="지도 확대" onClick={() => adjustZoom(1.25)}>+</button>
        <button title="축소" aria-label="지도 축소" onClick={() => adjustZoom(0.8)}>−</button>
        <button title="전체 보기" aria-label="지도 전체 보기" onClick={reset}>⊡</button>
        <span>{Math.round(zoom * 100)}%</span>
      </div>

      {graphLoading && <div className="warehouse-map__hint">Route Graph 불러오는 중…</div>}
      {graphError && <div className="warehouse-map__hint">Route Graph 오류: {graphError}</div>}
    </div>
  )
}
