"""Profile merge logic — used by check-in path."""

from __future__ import annotations

from handlers.profile_fetch import merge_profiles


def test_merge_profiles_with_both_sources() -> None:
    linkedin = {
        "id": "li-1",
        "name": "Test User",
        "headline": "Builder",
        "summary": "Working on cool things",
        "profile_url": "https://www.linkedin.com/in/test",
        "positions": [
            {
                "title": "Engineer",
                "company": "ACME",
                "location": None,
                "start": None,
                "end": None,
                "summary": None,
            }
        ],
        "skills": ["Python", "Rust"],
        "education": [],
    }
    github = {
        "login": "test-user",
        "name": "Test User",
        "bio": "Open source enthusiast",
        "profile_url": "https://github.com/test-user",
        "primary_languages": ["Python", "Go"],
        "repos": [
            {
                "name": "demo",
                "description": "demo project",
                "language": "Python",
                "stars": 5,
                "topics": ["ai"],
                "is_pinned": True,
            }
        ],
        "contribution_count": 50,
    }

    profile = merge_profiles(linkedin, github)
    assert profile.display_name == "Test User"
    assert profile.github_username == "test-user"
    skills = profile.skills
    assert "Python" in skills
    assert "Rust" in skills
    # GitHub-only languages still appear via the merged skill list.
    assert any(s.lower() == "go" for s in skills)


def test_merge_profiles_without_github() -> None:
    linkedin = {
        "id": "li-1",
        "name": "Solo User",
        "headline": "Solo",
        "summary": None,
        "profile_url": None,
        "positions": [],
        "skills": ["Python"],
        "education": [],
    }

    profile = merge_profiles(linkedin, None)
    assert profile.github is None
    assert profile.github_username is None
    assert profile.skills == ["Python"]
