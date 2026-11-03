"""Check the localization launch and installation metadata without ROS2."""

import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


PACKAGE = Path(__file__).resolve().parents[1]
LAUNCH = PACKAGE / 'launch' / 'tb3_localization_real.launch.py'
NAV2_SHARE = '/opt/ros/humble/share/nav2_bringup'


def launch_description(map_path, clock='false'):
    def action(kind):
        return lambda *args, **kwargs: SimpleNamespace(kind=kind, args=args, kwargs=kwargs)

    values = {'map': map_path, 'use_sim_time': clock}
    modules = {
        'ament_index_python': SimpleNamespace(),
        'ament_index_python.packages': SimpleNamespace(
            get_package_share_directory=lambda name: '/opt/ros/humble/share/' + name),
        'launch': SimpleNamespace(LaunchDescription=lambda actions: actions),
        'launch.actions': SimpleNamespace(
            IncludeLaunchDescription=action('include'), DeclareLaunchArgument=action('argument')),
        'launch.launch_description_sources': SimpleNamespace(
            PythonLaunchDescriptionSource=lambda path: path),
        'launch.substitutions': SimpleNamespace(LaunchConfiguration=lambda name: values[name]),
        'launch_ros': SimpleNamespace(),
        'launch_ros.actions': SimpleNamespace(Node=action('node')),
    }
    with patch.dict(sys.modules, modules):
        return runpy.run_path(str(LAUNCH))['generate_launch_description']()


class LocalizationLaunchTests(unittest.TestCase):
    def test_explicit_map_is_required_and_a_path_with_spaces_is_forwarded(self):
        map_path = '/tmp/room with spaces/new_map.yaml'
        actions = launch_description(map_path)
        declaration = next(a for a in actions if a.kind == 'argument' and a.args == ('map',))
        self.assertNotIn('default_value', declaration.kwargs)
        localization = next(a for a in actions if a.kind == 'include')
        self.assertEqual(dict(localization.kwargs['launch_arguments'])['map'], map_path)

    def test_original_localization_settings_and_rviz_clock_are_preserved(self):
        for clock in ('false', 'true'):
            with self.subTest(clock=clock):
                actions = launch_description('/tmp/new_map.yaml', clock)
                includes = [a for a in actions if a.kind == 'include']
                self.assertEqual(len(includes), 1)
                self.assertEqual(includes[0].args, (NAV2_SHARE + '/launch/localization_launch.py',))
                arguments = dict(includes[0].kwargs['launch_arguments'])
                self.assertEqual(arguments['params_file'], NAV2_SHARE + '/params/nav2_params.yaml')
                self.assertEqual(arguments['use_sim_time'], clock)
                nodes = [a for a in actions if a.kind == 'node']
                self.assertEqual(len(nodes), 1)
                self.assertEqual(nodes[0].kwargs['package'], 'rviz2')
                self.assertEqual(nodes[0].kwargs['arguments'],
                                 ['-d', NAV2_SHARE + '/rviz/nav2_default_view.rviz'])
                self.assertEqual(nodes[0].kwargs['parameters'], [{'use_sim_time': clock}])
                declaration = next(a for a in actions
                                   if a.kind == 'argument' and a.args == ('use_sim_time',))
                self.assertEqual(declaration.kwargs['default_value'], 'false')

    def test_launch_installation_preserves_resources_and_declares_dependencies(self):
        setup = Mock()
        previous = Path.cwd()
        try:
            os.chdir(PACKAGE)
            with patch.dict(sys.modules, {'setuptools': SimpleNamespace(setup=setup)}):
                runpy.run_path('setup.py')
        finally:
            os.chdir(previous)
        data = dict(setup.call_args.kwargs['data_files'])
        self.assertIn('launch/tb3_localization_real.launch.py', data['share/my_bot_fall/launch'])
        self.assertEqual(data['share/my_bot_fall/config'], ['config/params_robot.yaml'])
        self.assertEqual(data['share/my_bot_fall/models'], ['models/fall_pose_n.pt'])
        for sources in data.values():
            for source in sources:
                self.assertTrue((PACKAGE / source).is_file(), source)
        dependencies = {item.text for item in ET.parse(PACKAGE / 'package.xml').findall('exec_depend')}
        self.assertTrue({'launch', 'launch_ros', 'nav2_bringup', 'rviz2'} <= dependencies)


if __name__ == '__main__':
    unittest.main()
