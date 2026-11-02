from remote.instance_lock import GatewayInstanceLock


def test_gateway_instance_lock_name_is_stable_per_workspace(tmp_path):
    assert GatewayInstanceLock(tmp_path).name == GatewayInstanceLock(tmp_path).name


def test_gateway_instance_lock_name_isolated_per_workspace(tmp_path):
    assert GatewayInstanceLock(tmp_path / "one").name != GatewayInstanceLock(tmp_path / "two").name
