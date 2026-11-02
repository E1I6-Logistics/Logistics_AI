"""PC의 저장된 맵과 RViz만 실행한다. 로봇 제어/로컬라이제이션은 시작하지 않는다."""
from pathlib import Path

import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, OpaqueFunction, RegisterEventHandler
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAP = ROOT / 'robots_ws/Logistics_AMR/src/turtlebot3/turtlebot3_navigation2/map/logitle_map_fin.yaml'


def start(context):
    map_file = Path(LaunchConfiguration('map').perform(context)).expanduser().resolve()
    if not map_file.is_file():
        raise RuntimeError(f'로컬 맵 YAML을 찾을 수 없습니다: {map_file}')
    data = yaml.safe_load(map_file.read_text())
    image = data.get('image') if isinstance(data, dict) else None
    if not image or not (map_file.parent / image).is_file():
        raise RuntimeError(f'맵 YAML의 image 파일을 찾을 수 없습니다: {map_file}')
    map_server = Node(
        package='nav2_map_server', executable='map_server',
        namespace='fms_view', name='local_map_server', output='screen',
        parameters=[{'yaml_filename': str(map_file), 'frame_id': 'map',
                     'topic_name': '/fms_view/map', 'use_sim_time': False}],
    )
    manager = Node(
        package='nav2_lifecycle_manager', executable='lifecycle_manager',
        namespace='fms_view', name='local_map_lifecycle', output='screen',
        parameters=[{'autostart': True, 'use_sim_time': False,
                     # 같은 namespace 안의 상대 이름이어야 map_server의 bond ID와 일치한다.
                     'node_names': ['local_map_server']}],
    )
    rviz = Node(
        condition=IfCondition(LaunchConfiguration('use_rviz')),
        package='rviz2', executable='rviz2', output='screen',
        arguments=['-d', str(Path(__file__).with_name('fms_local_map.rviz'))],
        parameters=[{'use_sim_time': False}],
    )
    return [
        RegisterEventHandler(OnProcessExit(target_action=rviz, on_exit=[
            EmitEvent(event=Shutdown(reason='RViz를 닫아 로컬 맵 서버도 종료합니다.'))])),
        RegisterEventHandler(OnProcessExit(target_action=map_server, on_exit=[
            EmitEvent(event=Shutdown(reason='로컬 맵 서버가 종료됐습니다.'))])),
        RegisterEventHandler(OnProcessExit(target_action=manager, on_exit=[
            EmitEvent(event=Shutdown(reason='로컬 맵 활성화 관리자가 종료됐습니다.'))])),
        map_server, manager, rviz,
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('map', default_value=str(DEFAULT_MAP), description='PC 로컬 맵 YAML 경로'),
        DeclareLaunchArgument('use_rviz', default_value='true', description='RViz 창 실행 여부'),
        OpaqueFunction(function=start),
    ])
