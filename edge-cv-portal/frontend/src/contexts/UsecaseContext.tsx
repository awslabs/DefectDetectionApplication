import { createContext, useContext, useState, useEffect, ReactNode } from 'react';

interface UsecaseContextType {
  selectedUsecaseId: string | null;
  setSelectedUsecaseId: (usecaseId: string | null) => void;
}

const UsecaseContext = createContext<UsecaseContextType | undefined>(undefined);

const STORAGE_KEY = 'dda-selected-usecase-id';

export function UsecaseProvider({ children }: { children: ReactNode }) {
  const [selectedUsecaseId, setSelectedUsecaseIdState] = useState<string | null>(null);
  const [isInitialized, setIsInitialized] = useState(false);

  // Initialize from localStorage on mount
  useEffect(() => {
    const savedUsecaseId = localStorage.getItem(STORAGE_KEY);
    if (savedUsecaseId) {
      setSelectedUsecaseIdState(savedUsecaseId);
    }
    setIsInitialized(true);
  }, []);

  // Update localStorage when selection changes
  const setSelectedUsecaseId = (usecaseId: string | null) => {
    setSelectedUsecaseIdState(usecaseId);
    if (usecaseId) {
      localStorage.setItem(STORAGE_KEY, usecaseId);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  };

  if (!isInitialized) {
    return null; // Don't render until initialized
  }

  return (
    <UsecaseContext.Provider
      value={{
        selectedUsecaseId,
        setSelectedUsecaseId,
      }}
    >
      {children}
    </UsecaseContext.Provider>
  );
}

export function useUsecase() {
  const context = useContext(UsecaseContext);
  if (context === undefined) {
    throw new Error('useUsecase must be used within a UsecaseProvider');
  }
  return context;
}
