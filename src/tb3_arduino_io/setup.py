from setuptools import find_packages, setup

package_name = 'tb3_arduino_io'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='TB3 Team',
    maintainer_email='maintainer@example.org',
    description='ROS2 Humble USB serial bridge for RGB LED, buzzer and LCD.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'bridge = tb3_arduino_io.bridge:main',
        ],
    },
)
