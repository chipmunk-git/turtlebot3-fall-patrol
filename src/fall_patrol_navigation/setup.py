from setuptools import setup


package_name = 'fall_patrol_navigation'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='TB3 Team',
    maintainer_email='maintainer@example.org',
    description='A* navigation with bounded motion and localization checks.',
    license='LicenseRef-Unspecified',
    extras_require={'test': ['pytest']},
    entry_points={
        'console_scripts': [
            'navigation = fall_patrol_navigation.navigation_node:main',
        ],
    },
)
