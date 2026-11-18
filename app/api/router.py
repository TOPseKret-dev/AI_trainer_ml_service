from fastapi import APIRouter

from app.api.routes import evaluator, interviewer, question_bank, question_generation, transcription

api_router = APIRouter()
api_router.include_router(transcription.router)
api_router.include_router(interviewer.router)
api_router.include_router(evaluator.router)
api_router.include_router(question_bank.router)
api_router.include_router(question_generation.router)
