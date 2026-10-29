from setuptools import setup
setup(name='my_bot_fall', version='0.1.0', packages=['my_bot_fall'],
 data_files=[('share/ament_index/resource_index/packages',['resource/my_bot_fall']),('share/my_bot_fall',['package.xml']),('share/my_bot_fall/config',['config/params_robot.yaml']),('share/my_bot_fall/models',['models/fall_pose_n.pt'])],
 install_requires=['setuptools'], maintainer='student', maintainer_email='student@example.com',license='MIT',
 entry_points={'console_scripts':['patrol = my_bot_fall.fall_patrol:main','lying_detector = my_bot_fall.lying_detector_node:main','voice = my_bot_fall.voice_node:main']})
