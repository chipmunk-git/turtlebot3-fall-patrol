from setuptools import setup

package_name = "fall_patrol_bridge"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="GGo-ri",
    maintainer_email="speedmult@naver.com",
    description="낙상 의심 감지 서버와 ROS를 잇는 브리지",
    license="MIT",
    entry_points={
        "console_scripts": [
            "status_bridge = fall_patrol_bridge.status_bridge:main",
        ],
    },
)
