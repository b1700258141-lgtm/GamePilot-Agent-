"""靶场 profile 目录：合法集合、非法名称拒绝与缺陷说明。"""

import pytest

from gamepilot.lab import (
    FAULT_IDS,
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_CATALOG,
    PROFILE_NORMAL,
    PROFILES,
    UnknownProfileError,
    resolve_profile,
)


def test_profiles_are_normal_plus_the_three_faults() -> None:
    assert PROFILES == (PROFILE_NORMAL, *FAULT_IDS)
    assert FAULT_IDS == (
        FAULT_POTION_OVERHEAL,
        FAULT_POTION_NOT_CONSUMED,
        FAULT_RETALIATE_AFTER_DEATH,
    )
    assert len(set(PROFILES)) == len(PROFILES) == 4


def test_catalog_covers_exactly_the_known_profiles() -> None:
    assert set(PROFILE_CATALOG) == set(PROFILES)


def test_normal_profile_declares_no_fault() -> None:
    normal = PROFILE_CATALOG[PROFILE_NORMAL]

    assert normal.fault_id is None
    assert normal.profile_id == PROFILE_NORMAL
    assert normal.summary


@pytest.mark.parametrize("fault_id", FAULT_IDS)
def test_each_fault_profile_declares_its_own_fault(fault_id: str) -> None:
    profile = PROFILE_CATALOG[fault_id]

    assert profile.profile_id == fault_id
    assert profile.fault_id == fault_id
    assert profile.summary


def test_resolve_profile_returns_the_catalog_entry() -> None:
    assert resolve_profile(FAULT_POTION_OVERHEAL) is PROFILE_CATALOG[FAULT_POTION_OVERHEAL]
    assert resolve_profile(PROFILE_NORMAL) is PROFILE_CATALOG[PROFILE_NORMAL]


@pytest.mark.parametrize(
    "name", ["", "NORMAL", "normal ", " potion_overheal", "potion-overheal", "unknown"]
)
def test_unknown_profile_is_rejected_instead_of_falling_back_to_normal(name: str) -> None:
    with pytest.raises(UnknownProfileError) as excinfo:
        resolve_profile(name)

    assert excinfo.value.profile == name
    message = str(excinfo.value)
    # 报错信息要列出全部合法取值，调用方不用去翻源码。
    assert all(profile in message for profile in PROFILES)
