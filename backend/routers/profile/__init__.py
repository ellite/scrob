from fastapi import APIRouter

from . import account, translations, social, public_profile, highlights, stats

router = APIRouter()
router.include_router(account.router)
router.include_router(translations.router)
router.include_router(social.router)
router.include_router(public_profile.router)
router.include_router(highlights.router)
router.include_router(stats.router)
