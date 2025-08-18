#ifndef __EVICTABLE_LIST_H__
#define __EVICTABLE_LIST_H__

#include <list>
#include <mutex>
#include <functional>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>

#define MTX_LOCK std::lock_guard<std::mutex> lk(_mtx)
#define ITERATOR typename std::list<T>::const_iterator

template<typename T, typename Accumulation = uint8_t>
class EvictableList
{
public:
    /// @brief Create an evictable list only with the eviction strategy
    /// @param eviction_strategy The eviction strategy
    EvictableList(const std::function<ITERATOR(ITERATOR, ITERATOR, int32_t)>& eviction_strategy)
    {
        _eviction_strategy = eviction_strategy;
        _eviction_strategy_with_accumulation = nullptr;
        _accumulator = nullptr;
        _accumulation = 0;
    }

    /// @brief Create an evictable list with an eviction strategy and a method for computing "accumulation" of the list.
    /// @param eviction_strategy The eviction strategy
    /// @param accumulator The method that determines how much data is "accumulated" with a particular insertion (likely would be sizeof or length of a buffer)
    /// @param initial_value The initial value to set the accumluated value to (likely would be 0)
    EvictableList(const std::function<ITERATOR(ITERATOR, ITERATOR, int32_t, Accumulation)>& eviction_strategy, const std::function<Accumulation(const T&)>& accumulator, const Accumulation& initial_value)
    {
        _eviction_strategy = nullptr;
        _eviction_strategy_with_accumulation = eviction_strategy;
        _accumulator = accumulator;
        _accumulation = initial_value;
    }

    ~EvictableList()
    {
    }

    /// @brief Insert an element into the list.  After the insertion the provided eviction and accumulation methods will be  invoked.  The insertion operation will wait until the list is unlocked
    /// @param element The element to insert
    /// @return S_OK on success.  Error Code otherwise
    HRESULT Insert(const T& element)
    {
        MTX_LOCK;

        // insert the data
        _data.insert(_data.end(), element);

        // Invoke the appropriate eviction strategy callback
        typename std::list<T>::const_iterator erase_to = _data.cbegin();
        if(_accumulator)
        {
            _accumulation = _accumulation + _accumulator(element);
        }

        if(_eviction_strategy_with_accumulation != nullptr)
        {
            erase_to = _eviction_strategy_with_accumulation(_data.begin(), std::prev(_data.end()), _data.size(), _accumulation);
        }
        else if(_eviction_strategy != nullptr)
        {
            erase_to = _eviction_strategy(_data.begin(), std::prev(_data.end()), _data.size());
        }

        // Decrement the accumulator for each erased element if accumulator is provided
        // then erase from the beginning of the list to the point returned from eviction strategy function
        if(erase_to != _data.cbegin())
        {
            if(_accumulator)
            {
                for(auto iter = _data.cbegin(); iter != erase_to; iter++)
                {
                    _accumulation = _accumulation - _accumulator(*iter);
                }
            }

            _data.erase(_data.cbegin(), erase_to);
        }

        return S_OK;
    }

    /// @brief The number of elements in the list.  Not be to confused with Size() which is the total accumulaton of elements in the list.
    int32_t Count()
    {
        MTX_LOCK;
        return _data.size();
    }

    /// @brief The total accumulation (aka 'size') of the list.  Not to be confused with Count() which is the number of elements in the list.
    Accumulation Size()
    {
        MTX_LOCK;
        return _accumulation;
    }

    /// @brief Get a shallow copy of the underlying list.
    std::list<T> Snapshot()
    {
        MTX_LOCK;
        return _data;
    }

private:
    std::mutex _mtx;
    std::list<T> _data;

    std::function<ITERATOR(ITERATOR, ITERATOR, int32_t)> _eviction_strategy;
    std::function<ITERATOR(ITERATOR, ITERATOR, int32_t, Accumulation)> _eviction_strategy_with_accumulation;
    std::function<Accumulation(const T&)> _accumulator;
    Accumulation _accumulation; 
};

#endif